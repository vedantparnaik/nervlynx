import importlib.util

import pytest

from robot_core.drive import DriveTuning, SideShaper, SkidSteerDrive, mix_arcade, shape
from robot_core.hardware import (
  BTS7960Motor,
  HardwareUnavailable,
  MockBackend,
  TB6612Motor,
  create_backend,
  validate_motor_specs,
)
from robot_core.live import LiveRuntime
from robot_core.runtime import SimulatedClock

LEFT = [{"name": "FL", "rpwm": 12, "lpwm": 13}, {"name": "RL", "rpwm": 5, "lpwm": 6}]
RIGHT = [{"name": "FR", "rpwm": 18, "lpwm": 19, "invert": True}, {"name": "RR", "rpwm": 16, "lpwm": 20, "invert": True}]


def rover(**params) -> tuple[LiveRuntime, SkidSteerDrive]:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  drive = SkidSteerDrive(left=LEFT, right=RIGHT, **params)
  rt.add_node("drive", drive, rate_hz=50)
  rt.start()
  return rt, drive


def run_for(rt: LiveRuntime, seconds: float) -> None:
  for _ in range(int(round(seconds / 0.02))):
    rt.clock.advance_ms(20)
    rt.step()


def command(rt: LiveRuntime, left: float, right: float) -> None:
  rt.publish_external("cmd.drive", "DriveCommand", {"left": left, "right": right})


def test_shape_maps_requests_onto_the_usable_band() -> None:
  assert shape(0.005, 0.2) == 0.0
  assert shape(0.5, 0.2) == pytest.approx(0.6)
  assert shape(-1.0, 0.2) == -1.0
  assert shape(2.0, 0.2) == 1.0


def test_mix_arcade_normalises_together() -> None:
  assert mix_arcade(0.5, 0.0) == (0.5, 0.5)
  assert mix_arcade(0.0, 0.5) == (-0.5, 0.5)
  left, right = mix_arcade(1.0, 1.0)
  assert (left, right) == (0.0, 1.0)


def test_side_shaper_kicks_from_rest_then_ramps() -> None:
  side = SideShaper(DriveTuning(kick_duty=0.55, kick_s=0.18, slew_per_s=4.0))
  assert side.update(0.3, 0.02, 10.0) == 0.55
  assert side.kicks == 1
  assert side.update(0.3, 0.02, 10.1) == 0.55
  settled = [side.update(0.3, 0.02, 10.2 + i * 0.02) for i in range(10)]
  assert settled[-1] == pytest.approx(0.3)
  assert side.update(0.0, 0.02, 11.0) == 0.0


def test_side_shaper_skips_kick_just_after_stopping_and_coasts_on_reversal() -> None:
  t = DriveTuning(kick_rearm_s=0.5, slew_per_s=4.0)
  side = SideShaper(t)
  side.update(0.4, 0.02, 0.0)
  for i in range(20):
    side.update(0.4, 0.02, 0.02 * (i + 1))
  side.update(0.0, 0.02, 1.0)
  assert side.update(0.4, 0.02, 1.1) == pytest.approx(0.08)
  assert side.kicks == 1
  # Reversal first coasts the positive duty down to zero, then ramps the other way
  # without a kick because the wheels only just stopped.
  reversing = [side.update(-0.4, 0.02, 1.2 + i * 0.02) for i in range(4)]
  assert reversing[0] == 0.0
  assert reversing[1:] == pytest.approx([-0.08, -0.16, -0.24])
  assert side.kicks == 1


def test_forward_command_drives_bts7960_pins_with_inversion() -> None:
  rt, drive = rover(tuning={"kick_s": 0.0, "slew_per_s": 100.0, "min_duty": 0.0})
  command(rt, 0.5, 0.5)
  run_for(rt, 0.1)
  pins = drive.backend.pwm
  assert pins[12] == pytest.approx(0.5) and pins[13] == 0.0  # FL forward on RPWM
  assert pins[18] == 0.0 and pins[19] == pytest.approx(0.5)  # FR inverted: LPWM
  assert drive.status()["motors"] == {"FL": 0.5, "RL": 0.5, "FR": -0.5, "RR": -0.5}
  assert rt.metrics.value("nervlynx_motor_duty", {"motor": "FR"}) == pytest.approx(-0.5)


def test_deadman_stops_motors_when_commands_go_stale() -> None:
  rt, drive = rover(deadman_s=0.25)
  command(rt, 0.6, 0.6)
  run_for(rt, 0.1)
  assert drive.status()["applied"]["left"] > 0
  run_for(rt, 0.3)
  status = drive.status()
  assert status["applied"] == {"left": 0.0, "right": 0.0}
  assert status["deadman_active"] and status["deadman_stops"] == 1
  assert all(duty == 0.0 for duty in drive.backend.pwm.values())
  assert rt.metrics.value("nervlynx_drive_deadman_stops_total") == 1.0


def test_estop_zeroes_motors_ignores_commands_and_needs_a_fresh_command() -> None:
  rt, drive = rover(deadman_s=1.0)
  command(rt, 0.6, 0.6)
  run_for(rt, 0.1)
  rt.request_estop("test")
  assert all(duty == 0.0 for duty in drive.backend.pwm.values())
  run_for(rt, 0.04)
  command(rt, 0.9, 0.9)
  run_for(rt, 0.1)
  assert drive.status()["applied"] == {"left": 0.0, "right": 0.0}
  rt.request_estop_clear()
  run_for(rt, 0.1)
  assert rt.estop_engaged is False
  assert drive.status()["applied"] == {"left": 0.0, "right": 0.0}
  command(rt, 0.5, 0.5)
  run_for(rt, 0.1)
  assert drive.status()["applied"]["left"] > 0


def test_hard_stop_latch_holds_until_estop_is_cleared() -> None:
  rt, drive = rover(deadman_s=2.0)
  command(rt, 0.6, 0.6)
  run_for(rt, 0.1)
  drive.hard_stop()
  command(rt, 0.6, 0.6)
  run_for(rt, 0.1)
  assert drive.status()["applied"]["left"] == 0.0
  rt.request_estop("latched")
  run_for(rt, 0.04)
  rt.request_estop_clear()
  run_for(rt, 0.04)
  command(rt, 0.6, 0.6)
  run_for(rt, 0.1)
  assert drive.status()["hard_stopped"] is False
  assert drive.status()["applied"]["left"] > 0


def test_arcade_payload_max_speed_and_rejected_commands() -> None:
  rt, drive = rover(max_speed=0.5, tuning={"kick_s": 0.0, "slew_per_s": 100.0, "min_duty": 0.0})
  rt.publish_external("cmd.drive", "DriveCommand", {"linear": 1.0, "angular": 0.0})
  run_for(rt, 0.1)
  assert drive.status()["applied"]["left"] == pytest.approx(0.5)
  rt.publish_external("cmd.drive", "DriveCommand", {"speed": 1.0})
  rt.publish_external("cmd.drive", "DriveCommand", {"left": "fast", "right": 1})
  run_for(rt, 0.04)
  assert drive.status()["rejected_commands"] == 2
  assert "bad_command" in {f["kind"] for f in rt.snapshot()["faults"]}


def test_state_messages_measure_command_to_actuation_latency() -> None:
  rt, drive = rover()
  for _ in range(10):
    command(rt, 0.4, 0.4)
    run_for(rt, 0.1)
  latency = rt.snapshot()["topics"]["drive.state"]["latency_ms"]
  assert latency["count"] == 10
  assert latency["max"] <= 20.0


def test_teardown_releases_backend() -> None:
  rt, drive = rover()
  command(rt, 0.6, 0.6)
  run_for(rt, 0.1)
  rt.shutdown()
  assert drive.backend.closed is True
  assert all(duty == 0.0 for duty in drive.backend.pwm.values())


def test_tb6612_direction_pins() -> None:
  backend = MockBackend()
  motor = TB6612Motor(backend, "FL", 22, 23, 13, stby=25)
  assert backend.digital[25] is True
  motor.drive(0.4)
  assert (backend.digital[22], backend.digital[23], backend.pwm[13]) == (True, False, 0.4)
  motor.drive(-0.7)
  assert (backend.digital[22], backend.digital[23], backend.pwm[13]) == (False, True, 0.7)
  motor.stop()
  assert (backend.digital[22], backend.digital[23], backend.pwm[13]) == (False, False, 0.0)


def test_bts7960_releases_opposite_side_first() -> None:
  backend = MockBackend()
  motor = BTS7960Motor(backend, "FR", 18, 19, invert=True)
  motor.drive(0.3)
  assert backend.pwm == {18: 0.0, 19: 0.3}
  assert motor.duty == -0.3 and motor.command == 0.3
  motor.stop()
  assert motor.duty == 0.0


def test_motor_spec_validation() -> None:
  assert validate_motor_specs("bts7960", LEFT + RIGHT) == []
  shared_stby = [
    {"name": "FL", "in1": 22, "in2": 23, "pwm": 13, "stby": 25},
    {"name": "FR", "in1": 17, "in2": 27, "pwm": 18, "stby": 25},
  ]
  assert validate_motor_specs("tb6612", shared_stby) == []
  issues = validate_motor_specs("bts7960", [{"name": "A", "rpwm": 12, "lpwm": 13}, {"name": "B", "rpwm": 13, "lpwm": 40}])
  assert any("pin 13 is used by both" in i for i in issues)
  assert any("between 0 and 27" in i for i in issues)
  assert any("missing required pin lpwm" in i for i in validate_motor_specs("bts7960", [{"rpwm": 1}]))
  assert any("unknown fields" in i for i in validate_motor_specs("bts7960", [{"rpwm": 1, "lpwm": 2, "pwm": 3}]))
  assert any("shared enable/standby" in i for i in validate_motor_specs("tb6612", [{"in1": 1, "in2": 2, "pwm": 3, "stby": 3}]))
  assert validate_motor_specs("l298n", []) == ["unknown motor driver 'l298n'; expected one of bts7960, tb6612"]


def test_drive_constructor_rejects_bad_config_without_touching_hardware() -> None:
  with pytest.raises(ValueError, match="pin 12"):
    SkidSteerDrive(left=LEFT, right=[{"name": "X", "rpwm": 12, "lpwm": 21}])
  with pytest.raises(ValueError, match="backend"):
    SkidSteerDrive(left=LEFT, right=RIGHT, backend="arduino")
  with pytest.raises(ValueError, match="unknown tuning"):
    SkidSteerDrive(left=LEFT, right=RIGHT, tuning={"kick_power": 1})
  with pytest.raises(ValueError, match="max_speed"):
    SkidSteerDrive(left=LEFT, right=RIGHT, max_speed=1.5)


def test_backend_factory() -> None:
  assert isinstance(create_backend("mock"), MockBackend)
  with pytest.raises(ValueError):
    create_backend("arduino")
  if importlib.util.find_spec("RPi") is None:
    with pytest.raises(HardwareUnavailable):
      create_backend("rpi_gpio")
