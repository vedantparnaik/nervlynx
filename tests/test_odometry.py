import math

import pytest

from robot_core.live import LiveRuntime
from robot_core.live_config import build_live_runtime, validate_live_config
from robot_core.odometry import WheelOdometry
from robot_core.project import build_registry
from robot_core.runtime import SimulatedClock


def feed(node: WheelOdometry, counts: list[tuple[int, int]], step_ms: int = 100) -> list[dict]:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("odom", node)
  seen: list = []
  rt.subscribe("probe", "odom", seen.append)
  rt.start()
  for left, right in counts:
    rt.publish_external("link.encoders", "EncoderTicks", {"left_ticks": left, "right_ticks": right})
    rt.clock.advance_ms(step_ms)
    rt.step()
  return [m.payload for m in seen]


def test_straight_driving_and_turning_in_place() -> None:
  straight = feed(WheelOdometry(ticks_per_meter=1000), [(0, 0), (500, 500), (1000, 1000)])
  assert straight[-1]["x_m"] == pytest.approx(1.0) and straight[-1]["y_m"] == 0.0 and straight[-1]["heading_deg"] == 0.0
  assert straight[-1]["speed_mps"] == pytest.approx(5.0) and straight[-1]["distance_m"] == pytest.approx(1.0)
  quarter = math.pi / 2 * 0.25 / 2 * 1000  # wheel travel for a 90 degree turn on a 0.25 m track
  turned = feed(WheelOdometry(ticks_per_meter=1000), [(0, 0), (-round(quarter), round(quarter))])
  assert turned[-1]["heading_deg"] == pytest.approx(90.0, abs=0.3) and turned[-1]["x_m"] == pytest.approx(0.0, abs=1e-3)
  assert turned[-1]["yaw_rate_dps"] == pytest.approx(900.0, rel=0.01)


def test_wheel_size_inversion_and_bad_messages() -> None:
  node = WheelOdometry(wheel_diameter_m=0.065, ticks_per_rev=20 * 48, invert_left=True)
  assert node.ticks_per_meter == pytest.approx(960 / (math.pi * 0.065))
  out = feed(node, [(0, 0), (-4701, 4701)])
  assert out[-1]["x_m"] == pytest.approx(1.0, rel=0.01)
  with pytest.raises(ValueError, match="give ticks_per_meter"):
    WheelOdometry()


def test_encoders_from_the_link_become_odometry() -> None:
  reg = build_registry()
  cfg = {
    "nodes": [
      {"plugin": "esp32_link"},
      {"plugin": "wheel_odometry", "params": {"ticks_per_meter": 400}},
      {"plugin": "scripted_drive", "rate_hz": 20, "params": {"steps": [{"linear": 1.0, "duration_s": 2.0}], "loop": False}},
    ]
  }
  assert validate_live_config(cfg, reg) == []
  rt = build_live_runtime(cfg, reg, clock=SimulatedClock())
  rt.run(duration_s=1.0)
  odom = rt.nodes["wheel_odometry"].odometry()
  assert odom["x_m"] > 1.0 and odom["heading_deg"] == 0.0
