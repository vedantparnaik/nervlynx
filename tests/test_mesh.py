import socket
import time

import pytest

from robot_core.live import LiveNode, LiveRuntime
from robot_core.live_config import build_live_runtime, validate_live_config
from robot_core.mesh import (
  MemoryBus,
  MemoryTransport,
  MeshError,
  UdpTransport,
  decode,
  detect_device,
  encode,
  plan_mesh,
  validate_mesh_config,
)
from robot_core.project import build_registry
from robot_core.runtime import SimulatedClock

MOTORS = {"left": [{"name": "left", "rpwm": 1, "lpwm": 2}], "right": [{"name": "right", "rpwm": 3, "lpwm": 4}]}


def two_device_config(**mesh) -> dict:
  return {
    "name": "rover",
    "devices": {"rover": {"host": "pi@rover.local"}, "orin": {"hostname": "jetson"}},
    "mesh": {"transport": "udp", **mesh},
    "nodes": [
      {"name": "drive", "plugin": "skid_steer_drive", "rate_hz": 50, "params": MOTORS},
      {"name": "range", "plugin": "hcsr04_range", "params": {"trigger": 23, "echo": 24, "mock_distance_m": 0.8}},
      {
        "name": "planner",
        "plugin": "scripted_drive",
        "placement": "orin",
        "rate_hz": 20,
        "input_topics": ["range.front"],
        "params": {"steps": [{"linear": 0.5, "duration_s": 60.0}]},
      },
    ],
  }


def test_mesh_config_is_validated() -> None:
  assert validate_mesh_config(two_device_config()) == []
  cfg = two_device_config()
  del cfg["mesh"]
  assert validate_mesh_config(cfg) == ["nodes run on orin, rover but there is no mesh: section to connect them"]
  cfg = two_device_config(frames=["front"], port=0, typo=1)
  cfg["nodes"][2]["placement"] = "laptop"
  assert validate_mesh_config(cfg) == [
    "nodes[2].placement 'laptop' is not one of the devices: rover, orin",
    "unknown mesh key: typo",
    "mesh.port must be 1..65535",
    "mesh.frames needs transport: zenoh (camera frames are too big for UDP datagrams)",
  ]
  lonely = {"nodes": [{"plugin": "scripted_drive", "placement": "orin", "params": {"steps": [{"left": 0, "right": 0, "duration_s": 1}]}}]}
  assert validate_mesh_config(lonely) == ["nodes[0].placement needs a devices: section listing 'orin'"]
  reg = build_registry()
  assert validate_live_config(two_device_config(), reg) == []
  assert "mesh needs a devices: section naming the computers to connect" in validate_live_config({**lonely, "mesh": {}}, reg)


def test_devices_are_found_by_hostname() -> None:
  cfg = two_device_config()
  assert detect_device(cfg, "rover.local") == "rover"
  assert detect_device(cfg, "JETSON") == "orin"
  with pytest.raises(MeshError, match=r"this computer \(laptop\) is not one of the devices \(rover, orin\)"):
    detect_device(cfg, "laptop")


def test_plan_sends_only_what_other_devices_consume() -> None:
  cfg = two_device_config(share=["odom"])
  reg = build_registry()
  from robot_core.live_config import node_input_topics

  def topics(node):
    return node_input_topics(node, cfg, reg)

  rover = plan_mesh(cfg, "rover", topics)
  assert [n["name"] for n in rover.nodes] == ["drive", "range"]
  assert rover.out_topics == {"range.front", "odom"} and rover.in_topics == {"cmd.drive"}
  orin = plan_mesh(cfg, "orin", topics)
  assert orin.out_topics == {"cmd.drive", "odom"} and orin.in_topics == {"range.front"} and orin.peers == ("rover",)
  with pytest.raises(MeshError, match="--device 'laptop' is not one of"):
    plan_mesh(cfg, "laptop", topics)


def test_wire_format_signs_and_carries_binary() -> None:
  env = {"v": 1, "k": "frame", "name": "front"}
  key = b"shared-secret"
  wire = encode(env, key, b"\xff\xd8jpeg\nbytes")
  assert decode(wire, key) == (env, b"\xff\xd8jpeg\nbytes", True)
  assert decode(encode(env, None), None) == (env, None, False)
  with pytest.raises(MeshError, match="bad signature"):
    decode(wire[:-1] + b"X", key)
  with pytest.raises(MeshError, match="unsigned"):
    decode(encode(env, None), key)
  with pytest.raises(MeshError, match="protocol version"):
    decode(encode({"v": 99}, None), None)


class Robot:
  """Two devices of one robot, each with its own runtime, joined by a memory bus."""

  def __init__(self, cfg: dict, keys: tuple[str | None, str | None] = (None, None), bus: MemoryBus | None = None, monkeypatch=None):
    self.bus = bus or MemoryBus()
    reg = build_registry()
    self.devices: dict[str, LiveRuntime] = {}
    for device, key in zip(("rover", "orin"), keys):
      if monkeypatch is not None:
        if key is None:
          monkeypatch.delenv("NERVLYNX_MESH_KEY", raising=False)
        else:
          monkeypatch.setenv("NERVLYNX_MESH_KEY", key)
      self.devices[device] = build_live_runtime(cfg, reg, clock=SimulatedClock(), device=device, mesh_transport=MemoryTransport(self.bus))
    for rt in self.devices.values():
      rt.start()

  def run(self, seconds: float) -> None:
    for _ in range(int(round(seconds / 0.01))):
      for rt in self.devices.values():
        rt.clock.advance_ms(10)
        rt.step()

  def __getitem__(self, device: str) -> LiveRuntime:
    return self.devices[device]


def test_nodes_on_different_devices_talk_through_the_mesh() -> None:
  robot = Robot(two_device_config())
  robot.run(1.0)
  rover, orin = robot["rover"], robot["orin"]
  assert set(rover.nodes) == {"drive", "range", "mesh"} and set(orin.nodes) == {"planner", "mesh"}
  assert rover.nodes["drive"].status()["commands"] > 10 and rover.nodes["drive"].status()["applied"]["left"] > 0
  assert orin.snapshot()["topics"]["range.front"]["count"] > 5
  status = rover.nodes["mesh"].status()
  assert status["peers"]["orin"]["online"] is True and status["signed"] is False
  assert status["out_topics"] == ["range.front"] and status["in_topics"] == ["cmd.drive"]
  assert any(e.message == "mesh: orin joined" for e in rover.fault_events)


def test_estop_is_robot_wide() -> None:
  robot = Robot(two_device_config())
  robot.run(0.3)
  robot["orin"].request_estop("operator", source="http")
  robot.run(0.05)
  assert robot["rover"].estop_engaged and "operator (on orin)" in robot["rover"].snapshot()["estop"]["reason"]
  assert all(m.duty == 0.0 for m in robot["rover"].nodes["drive"].left_motors)
  robot["rover"].request_estop_clear(source="http")
  robot.run(2.0)
  assert not robot["rover"].estop_engaged and not robot["orin"].estop_engaged


class Stuck(LiveNode):
  input_topics = ("never",)


def test_a_device_that_cannot_clear_keeps_the_whole_robot_stopped() -> None:
  cfg = two_device_config()
  reg = build_registry()
  reg.register_live_node("stuck", Stuck)
  cfg["nodes"].append({"name": "stuck", "plugin": "stuck", "placement": "orin", "critical": True})
  bus = MemoryBus()
  rover = build_live_runtime(cfg, reg, clock=SimulatedClock(), device="rover", mesh_transport=MemoryTransport(bus))
  orin = build_live_runtime(cfg, reg, clock=SimulatedClock(), device="orin", mesh_transport=MemoryTransport(bus))
  robot = Robot.__new__(Robot)
  robot.devices = {"rover": rover, "orin": orin}
  for rt in (rover, orin):
    rt.start()
  robot.run(1.0)
  assert orin.estop_engaged and rover.estop_engaged  # the watchdog on orin stops the rover too
  rover.request_estop_clear(source="http")
  robot.run(0.2)
  assert not rover.estop_engaged and orin.estop_engaged  # orin refuses: its critical node is stale
  robot.run(2.0)
  assert rover.estop_engaged and "orin is still e-stopped" in rover.snapshot()["estop"]["reason"]


def test_signed_meshes_drop_strangers(monkeypatch) -> None:
  robot = Robot(two_device_config(), keys=("s3cret", "s3cret"), monkeypatch=monkeypatch)
  robot.run(0.5)
  assert robot["rover"].nodes["mesh"].status()["signed"] and robot["rover"].nodes["drive"].status()["commands"] > 0
  robot = Robot(two_device_config(), keys=("s3cret", "other"), monkeypatch=monkeypatch)
  robot.run(0.5)
  assert robot["rover"].nodes["drive"].status()["commands"] == 0
  assert robot["rover"].nodes["mesh"].status()["dropped"] > 0
  assert any("every device needs the same NERVLYNX_MESH_KEY" in e.message for e in robot["rover"].fault_events)


def test_other_robots_on_the_network_are_ignored() -> None:
  bus = MemoryBus()
  cfg = two_device_config()
  reg = build_registry()
  rover = build_live_runtime(cfg, reg, clock=SimulatedClock(), device="rover", mesh_transport=MemoryTransport(bus))
  other = two_device_config(robot="rover-2")
  stranger = build_live_runtime(other, reg, clock=SimulatedClock(), device="orin", mesh_transport=MemoryTransport(bus))
  robot = Robot.__new__(Robot)
  robot.devices = {"rover": rover, "orin": stranger}
  for rt in (rover, stranger):
    rt.start()
  robot.run(0.5)
  assert rover.nodes["drive"].status()["commands"] == 0 and rover.nodes["mesh"].status()["peers"] == {}


def test_a_lost_peer_is_reported_and_its_commands_stop() -> None:
  robot = Robot(two_device_config())
  robot.run(0.5)
  robot["orin"].shutdown()
  del robot.devices["orin"]
  robot.run(3.0)
  rover = robot["rover"]
  assert any(e.message.startswith("mesh: lost contact with orin") for e in rover.fault_events)
  assert rover.nodes["drive"].status()["deadman_active"] is True


def free_port() -> int:
  with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


def test_udp_transport_unicast_between_two_ports() -> None:
  pa, pb = free_port(), free_port()
  a = UdpTransport(port=pa, peers=[f"127.0.0.1:{pb}"])
  b = UdpTransport(port=pb, peers=[f"127.0.0.1:{pa}"])
  got: list[bytes] = []
  a.open(lambda data: None)
  b.open(got.append)
  try:
    a.send("msg/x", b"hello")
    a.send("msg/x", b"x" * 70_000)
    deadline = time.monotonic() + 2.0
    while not got and time.monotonic() < deadline:
      time.sleep(0.01)
    assert got == [b"hello"] and a.oversize == 1
  finally:
    a.close()
    b.close()
