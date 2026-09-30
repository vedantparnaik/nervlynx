"""Sensor nodes for common hobby parts, each with a mock twin for laptops and CI.

hcsr04_range  HC-SR04 ultrasonic distance -> range.<name> {"distance_m", "max_range_m", "hit"}
mpu6050_imu   MPU6050-family IMU over I2C -> imu {"accel_mps2", "gyro_dps", "temp_c"}

Payloads match the simulator's, so the same node code runs against real or simulated
sensors. Like motor nodes, they take `backend` (from `hardware.backend`); `auto` uses the
real part on a Raspberry Pi and the mock twin elsewhere.
"""

from __future__ import annotations

from typing import Any, Iterable

from robot_core.hardware import HardwareUnavailable, resolve_backend
from robot_core.live import LiveNode, NodeContext, Output

GRAVITY_MPS2 = 9.80665


def _bcm_pin(value: Any, label: str) -> int:
  if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 27:
    raise ValueError(f"{label} must be a BCM pin number between 0 and 27")
  return value


class HCSR04Range(LiveNode):
  """HC-SR04 ultrasonic ranger read through gpiozero's DistanceSensor.

  gpiozero times the echo on its own thread, so `tick` only reads the latest value and
  never blocks the executor. The echo pin outputs 5 V: wire it through a voltage divider
  (e.g. 1 kΩ + 2 kΩ) before it reaches the Pi's 3.3 V GPIO.
  """

  rate_hz = 15.0

  def __init__(
    self,
    *,
    trigger: int,
    echo: int,
    name: str = "front",
    topic: str | None = None,
    max_range_m: float = 2.0,
    backend: str = "mock",
    mock_distance_m: float | None = None,
    queue_len: int = 3,
  ) -> None:
    self.trigger = _bcm_pin(trigger, "trigger")
    self.echo = _bcm_pin(echo, "echo")
    if self.trigger == self.echo:
      raise ValueError("trigger and echo must be different pins")
    if max_range_m <= 0 or max_range_m > 4.0:
      raise ValueError("max_range_m must be in (0, 4] (the HC-SR04 is rated to about 4 m)")
    if mock_distance_m is not None and mock_distance_m < 0:
      raise ValueError("mock_distance_m must be >= 0")
    self.sensor_name = name
    self.topic = topic or f"range.{name}"
    self.max_range_m = float(max_range_m)
    self.backend_name = backend
    self.mock_distance_m = mock_distance_m
    self.queue_len = int(queue_len)
    self.resolved_backend: str | None = None
    self.distance_m: float | None = None
    self._device: Any = None

  def setup(self, ctx: NodeContext) -> None:
    self.resolved_backend = resolve_backend(self.backend_name)
    if self.backend_name == "auto":
      ctx.fault(f"hardware backend auto resolved to {self.resolved_backend}", severity="info", kind="hardware")
    if self.resolved_backend == "mock":
      return
    if self.resolved_backend != "gpiozero":
      raise HardwareUnavailable("hcsr04_range needs the gpiozero backend (sudo apt install python3-gpiozero python3-lgpio)")
    from gpiozero import DistanceSensor  # type: ignore[import-not-found]

    self._device = DistanceSensor(echo=self.echo, trigger=self.trigger, max_distance=self.max_range_m, queue_len=self.queue_len)

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    if self._device is None:
      distance = self.max_range_m if self.mock_distance_m is None else min(self.mock_distance_m, self.max_range_m)
    else:
      distance = min(max(float(self._device.distance), 0.0), self.max_range_m)
    self.distance_m = round(distance, 4)
    hit = distance < self.max_range_m - 1e-3
    return [(self.topic, "Range", {"distance_m": self.distance_m, "max_range_m": self.max_range_m, "hit": hit})]

  def teardown(self, ctx: NodeContext) -> None:
    if self._device is not None:
      self._device.close()
      self._device = None

  def status(self) -> dict[str, Any]:
    return {"distance_m": self.distance_m, "backend": self.resolved_backend or self.backend_name, "pins": {"trigger": self.trigger, "echo": self.echo}}


_WHO_AM_I = {0x68: "MPU6050", 0x70: "MPU6500", 0x71: "MPU9250", 0x73: "MPU9255"}
_ACCEL_RANGES_G = {2: 0, 4: 1, 8: 2, 16: 3}
_GYRO_RANGES_DPS = {250: 0, 500: 1, 1000: 2, 2000: 3}
_GYRO_LSB_PER_DPS = {250: 131.0, 500: 65.5, 1000: 32.8, 2000: 16.4}
_REG_PWR_MGMT_1, _REG_GYRO_CONFIG, _REG_ACCEL_CONFIG, _REG_DATA, _REG_WHO_AM_I = 0x6B, 0x1B, 0x1C, 0x3B, 0x75


def _signed16(high: int, low: int) -> int:
  value = (high << 8) | low
  return value - 0x10000 if value & 0x8000 else value


class MPU6050Imu(LiveNode):
  """MPU6050 (also MPU6500/9250 accel+gyro) over I2C with smbus2.

  Averages `calibrate_samples` gyro readings at start-up to remove bias, so keep the robot
  still while it starts. Each read is one 14-byte I2C transfer (about 1 ms at 100 kHz).
  """

  rate_hz = 50.0

  def __init__(
    self,
    *,
    bus: int = 1,
    address: int = 0x68,
    topic: str = "imu",
    accel_range_g: int = 2,
    gyro_range_dps: int = 250,
    calibrate_samples: int = 100,
    backend: str = "mock",
  ) -> None:
    if address not in (0x68, 0x69):
      raise ValueError("address must be 0x68 (AD0 low) or 0x69 (AD0 high)")
    if accel_range_g not in _ACCEL_RANGES_G:
      raise ValueError(f"accel_range_g must be one of {sorted(_ACCEL_RANGES_G)}")
    if gyro_range_dps not in _GYRO_RANGES_DPS:
      raise ValueError(f"gyro_range_dps must be one of {sorted(_GYRO_RANGES_DPS)}")
    if calibrate_samples < 0:
      raise ValueError("calibrate_samples must be >= 0")
    self.bus_number = int(bus)
    self.address = int(address)
    self.topic = topic
    self.accel_range_g = accel_range_g
    self.gyro_range_dps = gyro_range_dps
    self.calibrate_samples = int(calibrate_samples)
    self.backend_name = backend
    self.resolved_backend: str | None = None
    self.chip: str | None = None
    self.gyro_bias = [0.0, 0.0, 0.0]
    self.last: dict[str, Any] | None = None
    self._bus: Any = None
    self._accel_lsb_per_g = 32768.0 / accel_range_g
    self._gyro_lsb_per_dps = _GYRO_LSB_PER_DPS[gyro_range_dps]

  def _io_error(self, exc: OSError) -> HardwareUnavailable:
    return HardwareUnavailable(
      f"I2C transfer with 0x{self.address:02x} on bus {self.bus_number} failed ({exc}); "
      f"check SDA/SCL wiring and that `i2cdetect -y {self.bus_number}` shows the device"
    )

  def setup(self, ctx: NodeContext) -> None:
    self.resolved_backend = resolve_backend(self.backend_name)
    if self.backend_name == "auto":
      ctx.fault(f"hardware backend auto resolved to {self.resolved_backend}", severity="info", kind="hardware")
    if self.resolved_backend == "mock":
      self.chip = "mock"
      return
    try:
      from smbus2 import SMBus  # type: ignore[import-not-found]
    except ImportError as exc:
      raise HardwareUnavailable("mpu6050_imu needs smbus2: pip install smbus2") from exc
    self._bus = SMBus(self.bus_number)
    try:
      who = self._bus.read_byte_data(self.address, _REG_WHO_AM_I)
      if who not in _WHO_AM_I:
        raise HardwareUnavailable(f"device at 0x{self.address:02x} answered WHO_AM_I=0x{who:02x}, which is not an MPU6050-family IMU")
      self.chip = _WHO_AM_I[who]
      self._bus.write_byte_data(self.address, _REG_PWR_MGMT_1, 0x00)
      self._bus.write_byte_data(self.address, _REG_ACCEL_CONFIG, _ACCEL_RANGES_G[self.accel_range_g] << 3)
      self._bus.write_byte_data(self.address, _REG_GYRO_CONFIG, _GYRO_RANGES_DPS[self.gyro_range_dps] << 3)
      if self.calibrate_samples:
        sums = [0.0, 0.0, 0.0]
        for _ in range(self.calibrate_samples):
          _, gyro, _ = self._read_raw()
          sums = [s + g for s, g in zip(sums, gyro)]
        self.gyro_bias = [s / self.calibrate_samples for s in sums]
    except OSError as exc:
      self._bus.close()
      self._bus = None
      raise self._io_error(exc) from exc
    except HardwareUnavailable:
      self._bus.close()
      self._bus = None
      raise

  def _read_raw(self) -> tuple[list[float], list[float], float]:
    data = self._bus.read_i2c_block_data(self.address, _REG_DATA, 14)
    words = [_signed16(data[i], data[i + 1]) for i in range(0, 14, 2)]
    accel = [w / self._accel_lsb_per_g * GRAVITY_MPS2 for w in words[0:3]]
    gyro = [w / self._gyro_lsb_per_dps for w in words[4:7]]
    temp_c = words[3] / 340.0 + 36.53
    return accel, gyro, temp_c

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    if self._bus is None:
      accel, gyro, temp_c = [0.0, 0.0, GRAVITY_MPS2], [0.0, 0.0, 0.0], 25.0
    else:
      try:
        accel, gyro, temp_c = self._read_raw()
      except OSError as exc:
        raise self._io_error(exc) from exc
      gyro = [g - b for g, b in zip(gyro, self.gyro_bias)]
    self.last = {
      "accel_mps2": [round(v, 4) for v in accel],
      "gyro_dps": [round(v, 4) for v in gyro],
      "temp_c": round(temp_c, 2),
    }
    return [(self.topic, "Imu", self.last)]

  def teardown(self, ctx: NodeContext) -> None:
    if self._bus is not None:
      self._bus.close()
      self._bus = None

  def status(self) -> dict[str, Any]:
    return {
      "chip": self.chip,
      "backend": self.resolved_backend or self.backend_name,
      "address": f"0x{self.address:02x}",
      "gyro_bias_dps": [round(b, 4) for b in self.gyro_bias],
      "last": self.last,
    }
