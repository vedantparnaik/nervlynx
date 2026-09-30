import sys
import types

import pytest

from robot_core.drive import SkidSteerDrive
from robot_core.hardware import HardwareUnavailable, L298NMotor, MockBackend, validate_motor_specs
from robot_core.live import LiveRuntime
from robot_core.live_config import build_live_runtime, validate_live_config
from robot_core.project import build_registry
from robot_core.runtime import SimulatedClock
from robot_core.sensors import GRAVITY_MPS2, HCSR04Range, MPU6050Imu


def run_node(node, seconds: float = 0.2) -> tuple[LiveRuntime, list]:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("sensor", node)
  seen: list = []
  rt.add_message_listener(seen.append)
  rt.run(duration_s=seconds)
  return rt, seen


def test_l298n_with_enable_pin_sets_direction_and_pwm() -> None:
  backend = MockBackend()
  motor = L298NMotor(backend, "L", 5, 6, en=12)
  motor.drive(0.5)
  assert (backend.digital[5], backend.digital[6], backend.pwm[12]) == (True, False, 0.5)
  motor.drive(-0.25)
  assert (backend.digital[5], backend.digital[6], backend.pwm[12]) == (False, True, 0.25)
  motor.stop()
  assert (backend.digital[5], backend.digital[6], backend.pwm[12]) == (False, False, 0.0)


def test_l298n_with_jumpered_enable_puts_pwm_on_the_inputs() -> None:
  backend = MockBackend()
  motor = L298NMotor(backend, "R", 20, 21, invert=True)
  motor.drive(0.4)
  assert backend.pwm == {20: 0.0, 21: 0.4}
  motor.drive(-0.4)
  assert backend.pwm == {20: 0.4, 21: 0.0}


def test_l298n_specs_are_validated_like_the_other_drivers() -> None:
  assert validate_motor_specs("l298n", [{"name": "L", "in1": 5, "in2": 6, "en": 12}, {"name": "R", "in1": 20, "in2": 21}]) == []
  assert "pin 12 is used by both L.en and R.in1" in validate_motor_specs("l298n", [{"name": "L", "in1": 5, "in2": 6, "en": 12}, {"name": "R", "in1": 12, "in2": 21}])
  assert "L: missing required pin in2" in validate_motor_specs("l298n", [{"name": "L", "in1": 5}])
  drive = SkidSteerDrive(driver="l298n", left=[{"name": "L", "in1": 5, "in2": 6, "en": 12}], right=[{"name": "R", "in1": 20, "in2": 21, "en": 13}])
  assert drive.driver == "l298n"


def test_hcsr04_mock_reports_nothing_in_range_unless_told_otherwise() -> None:
  _, seen = run_node(HCSR04Range(trigger=23, echo=24))
  assert seen[-1].envelope.topic == "range.front"
  assert seen[-1].payload == {"distance_m": 2.0, "max_range_m": 2.0, "hit": False}
  _, seen = run_node(HCSR04Range(trigger=23, echo=24, name="rear", mock_distance_m=0.3))
  assert seen[-1].envelope.topic == "range.rear" and seen[-1].payload["hit"] is True


def test_hcsr04_reads_gpiozero_distance_sensor(monkeypatch) -> None:
  created: list = []

  class FakeDistanceSensor:
    def __init__(self, *, echo, trigger, max_distance, queue_len):
      self.args = (echo, trigger, max_distance, queue_len)
      self.distance = 0.42
      self.closed = False
      created.append(self)

    def close(self):
      self.closed = True

  monkeypatch.setitem(sys.modules, "gpiozero", types.SimpleNamespace(DistanceSensor=FakeDistanceSensor))
  node = HCSR04Range(trigger=23, echo=24, max_range_m=1.5, backend="gpiozero")
  _, seen = run_node(node)
  assert created[0].args == (24, 23, 1.5, 3)
  assert seen[-1].payload == {"distance_m": 0.42, "max_range_m": 1.5, "hit": True}
  assert created[0].closed is True


def test_hcsr04_rejects_backends_and_pins_it_cannot_use() -> None:
  with pytest.raises(HardwareUnavailable, match="needs the gpiozero backend"):
    HCSR04Range(trigger=23, echo=24, backend="rpi_gpio").setup(None)
  with pytest.raises(ValueError, match="different pins"):
    HCSR04Range(trigger=23, echo=23)
  with pytest.raises(ValueError, match="BCM pin"):
    HCSR04Range(trigger=40, echo=24)


class FakeSMBus:
  def __init__(self, who_am_i=0x68, fail_reads=False):
    self.who_am_i = who_am_i
    self.fail_reads = fail_reads
    self.writes: dict[int, int] = {}
    self.closed = False
    # accel (0, 0, +1 g at +-2 g), temp 25 C, gyro (+1, 0, -1) dps at +-250 dps
    self.block = [0x00, 0x00, 0x00, 0x00, 0x40, 0x00, 0xF0, 0xB0, 0x00, 0x83, 0x00, 0x00, 0xFF, 0x7D]

  def read_byte_data(self, address, register):
    assert register == 0x75
    return self.who_am_i

  def write_byte_data(self, address, register, value):
    self.writes[register] = value

  def read_i2c_block_data(self, address, register, length):
    if self.fail_reads:
      raise OSError(121, "Remote I/O error")
    assert (register, length) == (0x3B, 14)
    return list(self.block)

  def close(self):
    self.closed = True


def install_smbus(monkeypatch, bus: FakeSMBus) -> None:
  monkeypatch.setitem(sys.modules, "smbus2", types.SimpleNamespace(SMBus=lambda number: bus))


def test_mpu6050_configures_the_chip_and_removes_gyro_bias(monkeypatch) -> None:
  bus = FakeSMBus()
  install_smbus(monkeypatch, bus)
  node = MPU6050Imu(backend="gpiozero", calibrate_samples=10, accel_range_g=4)
  _, seen = run_node(node)
  assert bus.writes == {0x6B: 0x00, 0x1C: 1 << 3, 0x1B: 0x00}
  payload = seen[-1].payload
  assert payload["accel_mps2"] == [0.0, 0.0, round(2 * GRAVITY_MPS2, 4)]
  assert payload["gyro_dps"] == [0.0, 0.0, 0.0]
  assert payload["temp_c"] == pytest.approx(25.0, abs=0.01)
  assert node.status()["chip"] == "MPU6050" and node.status()["gyro_bias_dps"] == [1.0, 0.0, -1.0]
  assert bus.closed is True


def test_mpu6050_explains_wrong_chips_and_wiring_faults(monkeypatch) -> None:
  install_smbus(monkeypatch, FakeSMBus(who_am_i=0x12))
  with pytest.raises(HardwareUnavailable, match="WHO_AM_I=0x12"):
    MPU6050Imu(backend="gpiozero").setup(None)
  install_smbus(monkeypatch, FakeSMBus(fail_reads=True))
  with pytest.raises(HardwareUnavailable, match=r"i2cdetect -y 1"):
    MPU6050Imu(backend="gpiozero").setup(None)


def test_mpu6050_mock_reports_gravity() -> None:
  _, seen = run_node(MPU6050Imu())
  assert seen[-1].payload == {"accel_mps2": [0.0, 0.0, 9.8066], "gyro_dps": [0.0, 0.0, 0.0], "temp_c": 25.0}


def test_sensor_nodes_work_from_config_with_their_default_rates() -> None:
  reg = build_registry()
  cfg = {
    "nodes": [
      {"plugin": "hcsr04_range", "params": {"trigger": 23, "echo": 24}},
      {"plugin": "mpu6050_imu", "params": {"address": 0x69}},
    ]
  }
  assert validate_live_config(cfg, reg) == []
  rt = build_live_runtime(cfg, reg, clock=SimulatedClock())
  rt.run(duration_s=1.0)
  nodes = rt.snapshot()["nodes"]
  assert nodes["hcsr04_range"]["rate_hz"] == 15.0 and nodes["hcsr04_range"]["ticks"] >= 15
  assert nodes["mpu6050_imu"]["rate_hz"] == 50.0 and nodes["mpu6050_imu"]["status"]["address"] == "0x69"
  bad = {"nodes": [{"plugin": "mpu6050_imu", "params": {"address": 0x42}}]}
  assert "nodes[0] (mpu6050_imu): address must be 0x68 (AD0 low) or 0x69 (AD0 high)" in validate_live_config(bad, reg)
