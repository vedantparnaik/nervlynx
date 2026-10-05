import time

import pytest

from robot_core.heartbeat import Heartbeat
from robot_core.live import LiveNode, LiveRuntime
from robot_core.live_config import build_live_runtime, validate_live_config
from robot_core.project import build_registry
from robot_core.runtime import SimulatedClock

L298N = {"driver": "l298n", "left": [{"name": "l", "in1": 5, "in2": 6}], "right": [{"name": "r", "in1": 13, "in2": 26}]}


def started(node: Heartbeat) -> LiveRuntime:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("heartbeat", node)
  rt.start()
  return rt


def levels(rt: LiveRuntime, node: Heartbeat, steps: int) -> list[bool]:
  out = []
  for _ in range(steps):
    rt.step()
    out.append(node.backend.digital[node.pin])
    rt.clock.advance_ms(20)
  return out


def test_toggles_on_every_tick_while_the_robot_may_move() -> None:
  hb = Heartbeat(pin=26, frequency_hz=25)
  assert hb.rate_hz == 50.0
  rt = started(hb)
  assert hb.backend.digital[26] is False
  assert levels(rt, hb, 6) == [True, False, True, False, True, False]
  assert hb.rising_edges == 3 and hb.status()["toggling"] is True


def test_estop_holds_the_pin_low_until_it_is_cleared() -> None:
  hb = Heartbeat(pin=26)
  rt = started(hb)
  levels(rt, hb, 1)
  rt.request_estop("test", source="test")
  assert hb.backend.digital[26] is False
  assert levels(rt, hb, 5) == [False] * 5
  assert hb.status()["toggling"] is False
  rt.request_estop_clear(source="test")
  assert levels(rt, hb, 2) == [True, False]


def test_a_blocked_executor_stops_the_edges_and_the_stall_guard_drives_the_pin_low() -> None:
  hb = Heartbeat(pin=26)
  at_stall: dict = {}

  class Hang(LiveNode):
    def __init__(self) -> None:
      self.ticks = 0

    def tick(self, ctx):
      self.ticks += 1
      if self.ticks == 10:
        time.sleep(0.4)

  rt = LiveRuntime(stall_timeout_s=0.1)
  rt.add_node("heartbeat", hb)
  rt.add_node("hang", Hang(), rate_hz=50)

  def on_fault(event) -> None:
    if event.kind == "stall":
      at_stall.update(edges=hb.rising_edges, level=hb.backend.digital[26])

  rt.add_fault_listener(on_fault)
  rt.run(duration_s=0.8)
  assert at_stall["level"] is False and at_stall["edges"] > 0
  # The stall latched the e-stop, so the pin never toggled again.
  assert hb.rising_edges == at_stall["edges"]
  assert hb.backend is None and hb.level is False


def test_params_are_checked() -> None:
  for params, message in (({"pin": 28}, "BCM pin"), ({"pin": True}, "BCM pin"), ({"pin": 26, "frequency_hz": 500}, "frequency_hz"), ({"pin": 26, "backend": "x"}, "unknown backend")):
    with pytest.raises(ValueError, match=message):
      Heartbeat(**params)


def test_runs_from_config_on_mock_pins() -> None:
  reg = build_registry()
  cfg = {"nodes": [{"plugin": "heartbeat", "params": {"pin": 26, "frequency_hz": 10}}]}
  assert validate_live_config(cfg, reg) == []
  rt = build_live_runtime(cfg, reg, clock=SimulatedClock())
  rt.run(duration_s=1.0)
  assert rt.nodes["heartbeat"].rising_edges >= 10


def test_validation_rejects_a_pin_two_nodes_claim() -> None:
  reg = build_registry()
  clash = {"nodes": [{"name": "drive", "plugin": "skid_steer_drive", "params": L298N}, {"plugin": "heartbeat", "params": {"pin": 26}}]}
  assert validate_live_config(clash, reg) == ["nodes[1] (heartbeat): pin 26 (heartbeat) is already used by drive (r.in2)"]
  ranger = {"nodes": [{"plugin": "hcsr04_range", "params": {"trigger": 23, "echo": 24}}, {"plugin": "heartbeat", "params": {"pin": 24}}]}
  assert validate_live_config(ranger, reg) == ["nodes[1] (heartbeat): pin 24 (heartbeat) is already used by hcsr04_range (echo)"]


def test_pins_may_repeat_across_modes_and_devices() -> None:
  reg = build_registry()
  modes = {
    "nodes": [
      {"plugin": "hcsr04_range", "only": "robot", "params": {"trigger": 23, "echo": 24}},
      {"plugin": "heartbeat", "only": "sim", "params": {"pin": 23}},
    ]
  }
  assert validate_live_config(modes, reg) == []
  devices = {
    "devices": {"base": {}, "arm": {}},
    "mesh": {"transport": "udp"},
    "nodes": [
      {"plugin": "heartbeat", "params": {"pin": 26}},
      {"name": "arm_heartbeat", "plugin": "heartbeat", "placement": "arm", "params": {"pin": 26}},
    ],
  }
  assert validate_live_config(devices, reg) == []
  devices["nodes"][1]["placement"] = "base"
  assert validate_live_config(devices, reg) == ["nodes[1] (arm_heartbeat): pin 26 (heartbeat) is already used by heartbeat (heartbeat)"]
