import sys
import types

import pytest

from robot_core.hardware import HardwareUnavailable
from robot_core.live import LiveRuntime
from robot_core.live_config import validate_live_config
from robot_core.project import build_registry
from robot_core.runtime import SimulatedClock
from robot_core.servos import Pca9685Servos

PAN_TILT = [
  {"name": "pan", "channel": 0, "min_us": 500, "max_us": 2500, "max_speed_dps": 360},
  {"name": "tilt", "channel": 1, "min_deg": 30, "max_deg": 150, "home_deg": 90, "invert": True},
]


def servo_runtime(node: Pca9685Servos) -> LiveRuntime:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("arm", node)
  rt.start()
  return rt


def advance(rt: LiveRuntime, seconds: float) -> None:
  for _ in range(int(round(seconds / 0.02))):
    rt.clock.advance_ms(20)
    rt.step()


def ticks(data) -> int:
  return data[2] | (data[3] << 8)


def test_servo_specs_are_checked_before_any_hardware() -> None:
  for servos, message in (
    ([], "non-empty list"),
    ([{"name": "a", "channel": 16}], "channel must be 0..15"),
    ([{"name": "a", "channel": 0}, {"name": "a", "channel": 1}], "names must be unique"),
    ([{"name": "a", "channel": 0}, {"name": "b", "channel": 0}], "channels must be unique"),
    ([{"name": "a", "channel": 0, "min_us": 2000, "max_us": 1000}], "min_us < max_us"),
    ([{"name": "a", "channel": 0, "home_deg": 200}], "home_deg must be between"),
    ([{"name": "a", "channel": 0, "speed": 1}], "unknown fields speed"),
  ):
    with pytest.raises(ValueError, match=message):
      Pca9685Servos(servos=servos)
  with pytest.raises(ValueError, match="shorter than the 2000 us PWM period"):
    Pca9685Servos(servos=[{"name": "a", "channel": 0, "max_us": 2500}], frequency_hz=500)


def test_servos_start_home_and_move_at_their_speed_limit() -> None:
  arm = Pca9685Servos(servos=PAN_TILT)
  rt = servo_runtime(arm)
  assert arm.position == {"pan": 90.0, "tilt": 90.0}
  assert ticks(arm.pulses[0]) == round(1500 * 50 * 4096 / 1e6)
  rt.publish_external("cmd.servo", "ServoCommand", {"pan": 180, "tilt": 10})
  advance(rt, 0.1)
  # 360 deg/s for four 20 ms ticks (the first tick only starts the clock); tilt is clamped.
  assert arm.position["pan"] == pytest.approx(118.8) and arm.target["tilt"] == 30.0
  advance(rt, 0.5)
  assert arm.position == {"pan": 180.0, "tilt": 30.0}
  assert ticks(arm.pulses[0]) == round(2500 * 50 * 4096 / 1e6)
  assert ticks(arm.pulses[1]) == round(2000 * 50 * 4096 / 1e6)  # inverted: min_deg is max_us
  rt.publish_external("cmd.servo", "ServoCommand", {"name": "pan", "deg": 0})
  rt.publish_external("cmd.servo", "ServoCommand", {"elbow": 10})
  advance(rt, 1.0)
  assert arm.position["pan"] == 0.0
  assert any("servo command rejected: elbow=10" in e.message for e in rt.fault_events)


def test_estop_holds_servos_where_they_are() -> None:
  arm = Pca9685Servos(servos=PAN_TILT)
  rt = servo_runtime(arm)
  rt.publish_external("cmd.servo", "ServoCommand", {"pan": 180})
  advance(rt, 0.1)
  rt.request_estop("test", source="test")
  advance(rt, 0.2)
  held = arm.position["pan"]
  assert 90.0 < held < 180.0 and arm.target["pan"] == held
  rt.publish_external("cmd.servo", "ServoCommand", {"pan": 0})
  advance(rt, 0.2)
  assert arm.position["pan"] == held
  rt.request_estop_clear(source="test")
  advance(rt, 0.2)
  assert arm.position["pan"] == held


def test_relax_cuts_the_pulses_until_the_next_command() -> None:
  arm = Pca9685Servos(servos=PAN_TILT, on_stop="relax")
  rt = servo_runtime(arm)
  rt.request_estop("test", source="test")
  advance(rt, 0.04)
  assert arm.pulses == {0: None, 1: None} and arm.relaxed
  rt.request_estop_clear(source="test")
  advance(rt, 0.2)
  assert arm.relaxed
  rt.publish_external("cmd.servo", "ServoCommand", {"pan": 100})
  advance(rt, 0.2)
  assert arm.pulses[0] is not None and arm.pulses[1] is None


class FakePca:
  def __init__(self, fail=False):
    self.regs = {0x00: 0x11}
    self.writes: list = []
    self.blocks: dict = {}
    self.fail = fail
    self.closed = False

  def write_byte_data(self, address, register, value):
    if self.fail:
      raise OSError(121, "Remote I/O error")
    self.writes.append((register, value))
    self.regs[register] = value

  def read_byte_data(self, address, register):
    return self.regs.get(register, 0)

  def write_i2c_block_data(self, address, register, data):
    self.blocks[register] = list(data)

  def close(self):
    self.closed = True


def test_pca9685_is_configured_for_50_hz_and_written_per_channel(monkeypatch) -> None:
  bus = FakePca()
  monkeypatch.setitem(sys.modules, "smbus2", types.SimpleNamespace(SMBus=lambda number: bus))
  monkeypatch.setattr("robot_core.servos.time.sleep", lambda s: None)
  arm = Pca9685Servos(servos=PAN_TILT, backend="gpiozero", on_stop="relax")
  rt = servo_runtime(arm)
  assert (0xFE, 121) in bus.writes  # prescale for 50 Hz from a 25 MHz oscillator
  assert bus.writes[-1] == (0x00, 0x01 | 0x80 | 0x20)  # restart with register auto-increment
  assert ticks(bus.blocks[0x06]) == 307 and 0x06 + 4 in bus.blocks
  rt.shutdown()
  assert bus.blocks[0x06] == [0, 0, 0, 0x10] and bus.closed


def test_a_missing_board_explains_the_wiring(monkeypatch) -> None:
  monkeypatch.setitem(sys.modules, "smbus2", types.SimpleNamespace(SMBus=lambda number: FakePca(fail=True)))
  with pytest.raises(HardwareUnavailable, match="no PCA9685 answered at 0x40 on I2C bus 1"):
    Pca9685Servos(servos=PAN_TILT, backend="gpiozero").setup(types.SimpleNamespace(fault=lambda *a, **k: None))


def test_wizard_finds_travel_limits_by_jogging() -> None:
  arm = Pca9685Servos(servos=PAN_TILT)
  rt = servo_runtime(arm)
  ctx = rt._slots["arm"].ctx
  arm.calibrate("jog", {"servo": "pan", "us": 620}, ctx)
  assert ticks(arm.pulses[0]) == round(620 * 50 * 4096 / 1e6)
  arm.calibrate("set_limit", {"servo": "pan", "which": "min"}, ctx)
  arm.calibrate("jog", {"servo": "pan", "us": 2380}, ctx)
  out = arm.calibrate("set_limit", {"servo": "pan", "which": "max"}, ctx)
  assert out["servos"][0]["min_us"] == 620.0 and out["servos"][0]["max_us"] == 2380.0
  arm.calibrate("jog", {"servo": "pan", "us": 1450}, ctx)
  arm.calibrate("set_limit", {"servo": "pan", "which": "home"}, ctx)
  assert arm.calibration() == {"servos": [{"name": "pan", "min_us": 620.0, "max_us": 2380.0, "home_deg": pytest.approx(84.89, abs=0.01)}]}
  arm.calibrate("jog", {"servo": "pan", "us": 2350}, ctx)
  with pytest.raises(ValueError, match="at least 100 us apart"):
    arm.calibrate("set_limit", {"servo": "pan", "which": "min"}, ctx)
  rt.request_estop("test", source="test")
  advance(rt, 0.04)
  with pytest.raises(ValueError, match="clear the e-stop first"):
    arm.calibrate("jog", {"servo": "pan", "us": 1500}, ctx)


def test_servos_from_config() -> None:
  reg = build_registry()
  cfg = {"nodes": [{"name": "arm", "plugin": "pca9685_servos", "params": {"servos": PAN_TILT, "address": 0x41}}]}
  assert validate_live_config(cfg, reg) == []
  bad = {"nodes": [{"name": "arm", "plugin": "pca9685_servos", "params": {"servos": [{"name": "pan", "channel": 20}]}}]}
  assert validate_live_config(bad, reg) == ["nodes[0] (arm): servo pan: channel must be 0..15"]
