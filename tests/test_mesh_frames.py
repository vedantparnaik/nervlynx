import socket
import time

import pytest
from typer.testing import CliRunner

from robot_core.camera import frames
from robot_core.live_config import build_live_runtime
from robot_core.mesh import MemoryBus, MemoryTransport
from robot_core.nervlynx_cli import app
from robot_core.project import build_registry
from robot_core.runtime import SimulatedClock

MOTORS = {"left": [{"name": "left", "rpwm": 1, "lpwm": 2}], "right": [{"name": "right", "rpwm": 3, "lpwm": 4}]}


def camera_robot(transport: str = "zenoh") -> dict:
  return {
    "name": "watcher",
    "devices": {"rover": {"host": "pi@rover.local"}, "orin": {"host": "nvidia@orin.local"}},
    "mesh": {"transport": transport, "frames": ["front"], "frame_fps": 30},
    "nodes": [
      {"name": "drive", "plugin": "skid_steer_drive", "rate_hz": 50, "params": MOTORS},
      {"name": "camera", "plugin": "camera", "params": {"name": "front", "width": 64, "height": 48, "fps": 20}},
      {"name": "planner", "plugin": "scripted_drive", "placement": "orin", "rate_hz": 20, "params": {"steps": [{"linear": 0.3, "duration_s": 60}]}},
    ],
  }


def wait_for(predicate, step, timeout_s: float = 5.0) -> None:
  deadline = time.monotonic() + timeout_s
  while not predicate():
    assert time.monotonic() < deadline, "timed out"
    step()
    time.sleep(0.005)


def test_camera_frames_reach_the_other_device() -> None:
  bus = MemoryBus()
  reg = build_registry()
  cfg = camera_robot()
  rover = build_live_runtime(cfg, reg, clock=SimulatedClock(), device="rover", mesh_transport=MemoryTransport(bus))
  orin = build_live_runtime(cfg, reg, clock=SimulatedClock(), device="orin", mesh_transport=MemoryTransport(bus))
  assert rover.nodes["mesh"].plan.frames_out == ("front",) and orin.nodes["mesh"].plan.frames_in == ("front",)
  rover.start()
  orin.start()

  def step() -> None:
    for rt in (rover, orin):
      rt.clock.advance_ms(10)
      rt.step()

  try:
    remote = orin.nodes["mesh"]._frames_in["front"]
    wait_for(lambda: remote.latest() is not None, step)
    frame = remote.latest()
    assert frame.content_type == "image/png" and (frame.width, frame.height) == (64, 48)
    assert frame.data.startswith(b"\x89PNG") and frames("front") is remote
    assert orin.nodes["mesh"].status()["frames_in"] == ["front"]
  finally:
    rover.shutdown()
    orin.shutdown()
  assert frames("front") is None


def free_tcp_port() -> int:
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


def test_zenoh_transport_carries_commands_between_devices() -> None:
  pytest.importorskip("zenoh")
  from robot_core.mesh_zenoh import ZenohTransport

  port = free_tcp_port()
  reg = build_registry()
  cfg = camera_robot()
  cfg["mesh"].pop("frames")
  rover = build_live_runtime(cfg, reg, device="rover", mesh_transport=ZenohTransport(robot="watcher", listen=[f"tcp/127.0.0.1:{port}"], scouting=False))
  orin = build_live_runtime(cfg, reg, device="orin", mesh_transport=ZenohTransport(robot="watcher", connect=[f"tcp/127.0.0.1:{port}"], scouting=False))
  rover.start()
  orin.start()
  try:
    wait_for(lambda: rover.nodes["drive"].status()["commands"] > 3, lambda: (rover.step(), orin.step()), timeout_s=10.0)
    status = rover.nodes["mesh"].status()
    assert status["transport"] == "zenoh" and status["peers"]["orin"]["online"]
  finally:
    rover.shutdown()
    orin.shutdown()


def test_zenoh_missing_is_explained(monkeypatch) -> None:
  import sys

  from robot_core.mesh_zenoh import ZenohTransport

  monkeypatch.setitem(sys.modules, "zenoh", None)
  with pytest.raises(ModuleNotFoundError, match=r"nervlynx\[mesh\]"):
    ZenohTransport(robot="r").open(lambda data: None)


def test_deploy_to_a_device_uses_its_host_and_runs_its_nodes(tmp_path, monkeypatch) -> None:
  import yaml

  from robot_core import remote

  (tmp_path / "robot.yaml").write_text(yaml.safe_dump(camera_robot(), sort_keys=False), encoding="utf-8")
  calls: list = []
  real_deploy = remote.deploy
  monkeypatch.setattr(remote, "deploy", lambda target, project, **kw: real_deploy(target, project, **{**kw, "runner": lambda argv: calls.append(list(argv)) or 0}))
  result = CliRunner().invoke(app, ["deploy", "--project", str(tmp_path), "--device", "orin", "--service", "--no-validate"])
  assert result.exit_code == 0, result.stdout
  assert calls[0][:2] == ["ssh", "nvidia@orin.local"]
  assert "robot.yaml --quiet --device orin" in calls[2][2]
  missing = CliRunner().invoke(app, ["deploy", "--project", str(tmp_path), "--device", "laptop"])
  assert missing.exit_code == 2 and "no device named 'laptop' (devices: rover, orin)" in missing.stdout
