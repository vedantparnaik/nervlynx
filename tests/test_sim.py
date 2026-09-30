import pytest

from robot_core.live import LiveRuntime
from robot_core.runtime import SimulatedClock
from robot_core.sim import ScriptedDriveSource, SkidSteerSim


def collect(rt: LiveRuntime, topic: str) -> list:
  out: list = []
  rt.add_message_listener(lambda m: out.append(m) if m.envelope.topic == topic else None)
  return out


def test_scripted_source_walks_steps_and_loops() -> None:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node(
    "pattern",
    ScriptedDriveSource(steps=[{"left": 0.5, "right": 0.5, "duration_s": 0.2}, {"linear": 0.0, "angular": 0.3, "duration_s": 0.1}]),
    rate_hz=20,
  )
  cmds = collect(rt, "cmd.drive")
  rt.run(duration_s=0.6)
  steps = [m.payload["step"] for m in cmds]
  assert steps[:6] == [0, 0, 0, 0, 1, 1]
  assert steps[6] == 0
  assert cmds[4].payload == {"linear": 0.0, "angular": 0.3, "step": 1}


def test_scripted_source_goes_quiet_after_a_one_shot_script() -> None:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("pattern", ScriptedDriveSource(steps=[{"left": 0.2, "right": 0.2, "duration_s": 0.1}], loop=False, start_delay_s=0.1), rate_hz=20)
  cmds = collect(rt, "cmd.drive")
  rt.run(duration_s=1.0)
  assert len(cmds) == 2
  with pytest.raises(ValueError):
    ScriptedDriveSource(steps=[{"left": 1, "duration_s": 1}])


def _plant_after(left: float, right: float, seconds: float = 2.0) -> SkidSteerSim:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  plant = rt.add_node("plant", SkidSteerSim(odom_every_n_ticks=10), rate_hz=50)
  rt.start()
  rt.publish_external("drive.state", "DriveState", {"left_applied": left, "right_applied": right})
  for _ in range(int(seconds * 50)):
    rt.clock.advance_ms(20)
    rt.step()
  return plant


def test_plant_drives_straight_under_equal_duty() -> None:
  plant = _plant_after(0.6, 0.6)
  odom = plant.odometry()
  assert odom["x_m"] > 0.4
  assert abs(odom["y_m"]) < 1e-6
  assert odom["heading_deg"] == 0.0
  assert odom["speed_mps"] == pytest.approx(0.6 * (0.6 - 0.075) / 0.925, rel=0.02)


def test_plant_pivots_under_opposite_duty_and_ignores_duty_below_stiction() -> None:
  pivot = _plant_after(-0.5, 0.5, seconds=0.5)
  assert abs(pivot.odometry()["speed_mps"]) < 1e-6
  assert pivot.odometry()["heading_deg"] > 20
  still = _plant_after(0.1, 0.1)
  assert still.odometry()["distance_m"] == 0.0
