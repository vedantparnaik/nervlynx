"""`pca9685_servos`: hobby servos on a PCA9685 16-channel I2C PWM board.

Linux can't time servo pulses in software without jitter, so servos go through a PCA9685
(which generates the pulses itself) or a microcontroller. Commands on `cmd.servo` are
angles in degrees by servo name:

  {"pan": 45, "tilt": 100}          move these servos
  {"name": "pan", "deg": 45}        the same, for one servo

Each servo maps its pulse limits (`min_us`/`max_us`, found with the calibration wizard)
onto `min_deg`/`max_deg`, starts at `home_deg`, and moves at most `max_speed_dps`, so it
never slams across its range. Angles outside the limits are clamped. On e-stop and at
shutdown the servos hold where they are (`on_stop: hold`) or go limp (`relax`); commands
are ignored until the e-stop clears. State is published on `servo.state`.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

from robot_core.hardware import HardwareUnavailable, resolve_backend
from robot_core.live import LiveNode, NodeContext, Output
from robot_core.runtime import RuntimeMessage

_MODE1, _MODE2, _PRESCALE, _LED0_ON_L = 0x00, 0x01, 0xFE, 0x06
_RESTART, _SLEEP, _AUTO_INCREMENT, _ALLCALL, _OUTDRV = 0x80, 0x10, 0x20, 0x01, 0x04
_FULL_OFF = [0x00, 0x00, 0x00, 0x10]
_PULSE_BOUNDS_US = (400.0, 2600.0)
_SPEC_KEYS = {"name", "channel", "min_us", "max_us", "min_deg", "max_deg", "home_deg", "max_speed_dps", "invert"}


@dataclass
class Servo:
  name: str
  channel: int
  min_us: float = 1000.0
  max_us: float = 2000.0
  min_deg: float = 0.0
  max_deg: float = 180.0
  home_deg: float | None = None
  max_speed_dps: float = 360.0
  invert: bool = False

  @classmethod
  def parse(cls, raw: Any, idx: int) -> Servo:
    if not isinstance(raw, dict):
      raise ValueError(f"servos[{idx}] must be a mapping like {{name: pan, channel: 0}}")
    unknown = sorted(set(raw) - _SPEC_KEYS)
    if unknown:
      raise ValueError(f"servos[{idx}]: unknown fields {', '.join(unknown)}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
      raise ValueError(f"servos[{idx}].name must be a non-empty string")
    channel = raw.get("channel")
    if isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel <= 15:
      raise ValueError(f"servo {name}: channel must be 0..15")
    numbers = {}
    for key in ("min_us", "max_us", "min_deg", "max_deg", "home_deg", "max_speed_dps"):
      value = raw.get(key)
      if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
        raise ValueError(f"servo {name}: {key} must be a number")
      if value is not None:
        numbers[key] = float(value)
    servo = cls(name=name, channel=channel, invert=bool(raw.get("invert", False)), **numbers)
    servo.check()
    return servo

  def check(self) -> None:
    lo, hi = _PULSE_BOUNDS_US
    if not lo <= self.min_us < self.max_us <= hi or self.max_us - self.min_us < 100:
      raise ValueError(f"servo {self.name}: need {lo:.0f} <= min_us < max_us <= {hi:.0f}, at least 100 us apart")
    if self.min_deg >= self.max_deg:
      raise ValueError(f"servo {self.name}: min_deg must be below max_deg")
    if self.home_deg is not None and not self.min_deg <= self.home_deg <= self.max_deg:
      raise ValueError(f"servo {self.name}: home_deg must be between min_deg and max_deg")
    if self.max_speed_dps <= 0:
      raise ValueError(f"servo {self.name}: max_speed_dps must be > 0")

  @property
  def home(self) -> float:
    return self.home_deg if self.home_deg is not None else (self.min_deg + self.max_deg) / 2

  def clamp(self, deg: float) -> float:
    return max(self.min_deg, min(self.max_deg, deg))

  def pulse_us(self, deg: float) -> float:
    fraction = (self.clamp(deg) - self.min_deg) / (self.max_deg - self.min_deg)
    if self.invert:
      fraction = 1.0 - fraction
    return self.min_us + fraction * (self.max_us - self.min_us)

  def degrees(self, pulse_us: float) -> float:
    fraction = (pulse_us - self.min_us) / (self.max_us - self.min_us)
    if self.invert:
      fraction = 1.0 - fraction
    return self.min_deg + fraction * (self.max_deg - self.min_deg)


class Pca9685Servos(LiveNode):
  """Positions servos on a PCA9685 over I2C (smbus2), or records pulses with the mock backend."""

  rate_hz = 50.0

  def __init__(
    self,
    *,
    servos: list[dict[str, Any]],
    bus: int = 1,
    address: int = 0x40,
    frequency_hz: float = 50.0,
    oscillator_hz: float = 25_000_000.0,
    command_topic: str = "cmd.servo",
    state_topic: str = "servo.state",
    on_stop: str = "hold",
    state_every_n_ticks: int = 5,
    backend: str = "mock",
  ) -> None:
    if not isinstance(servos, list) or not servos:
      raise ValueError("servos must be a non-empty list like [{name: pan, channel: 0}]")
    parsed = [Servo.parse(raw, idx) for idx, raw in enumerate(servos)]
    for field in ("name", "channel"):
      values = [getattr(s, field) for s in parsed]
      dupes = sorted({str(v) for v in values if values.count(v) > 1})
      if dupes:
        raise ValueError(f"servo {field}s must be unique (repeated: {', '.join(dupes)})")
    if not 0x40 <= address <= 0x7F:
      raise ValueError("address must be 0x40..0x7f (0x40 unless the A0-A5 pads are bridged)")
    if not 24.0 <= frequency_hz <= 1526.0:
      raise ValueError("frequency_hz must be 24..1526 (50 for hobby servos)")
    if on_stop not in ("hold", "relax"):
      raise ValueError("on_stop must be 'hold' or 'relax'")
    period_us = 1e6 / frequency_hz
    if max(s.max_us for s in parsed) >= period_us:
      raise ValueError(f"max_us must be shorter than the {period_us:.0f} us PWM period at {frequency_hz} Hz")
    self.servos = {s.name: s for s in parsed}
    self.bus_number = int(bus)
    self.address = int(address)
    self.frequency_hz = float(frequency_hz)
    self.oscillator_hz = float(oscillator_hz)
    self.command_topic = command_topic
    self.state_topic = state_topic
    self.input_topics = (command_topic,)
    self.on_stop = on_stop
    self.state_every_n_ticks = max(1, int(state_every_n_ticks))
    self.backend_name = backend
    self.resolved_backend: str | None = None
    self.position = {name: s.home for name, s in self.servos.items()}
    self.target = dict(self.position)
    self.pulses: dict[int, list[int] | None] = {}
    self._written: dict[str, float | None] = {name: None for name in self.servos}
    self._bus: Any = None
    self._lock = threading.Lock()
    self._last_ns: int | None = None
    self._ticks = 0
    self._rejected = 0
    self._calibrated: set[str] = set()

  # ------------------------------------------------------------------ hardware

  def setup(self, ctx: NodeContext) -> None:
    self.resolved_backend = resolve_backend(self.backend_name)
    if self.backend_name == "auto":
      ctx.fault(f"hardware backend auto resolved to {self.resolved_backend}", severity="info", kind="hardware")
    if self.resolved_backend != "mock":
      try:
        from smbus2 import SMBus  # type: ignore[import-not-found]
      except ImportError as exc:
        raise HardwareUnavailable("pca9685_servos needs smbus2: pip install smbus2 (or sudo apt install python3-smbus2)") from exc
      self._bus = SMBus(self.bus_number)
      try:
        self._init_chip()
      except OSError as exc:
        self._bus.close()
        self._bus = None
        raise HardwareUnavailable(
          f"no PCA9685 answered at 0x{self.address:02x} on I2C bus {self.bus_number} ({exc}); "
          f"check SDA/SCL/VCC wiring and `i2cdetect -y {self.bus_number}`"
        ) from exc
    for servo in self.servos.values():
      self._write_servo(servo, self.position[servo.name])

  def _init_chip(self) -> None:
    bus, addr = self._bus, self.address
    bus.write_byte_data(addr, _MODE2, _OUTDRV)
    bus.write_byte_data(addr, _MODE1, _ALLCALL)
    time.sleep(0.005)
    awake = bus.read_byte_data(addr, _MODE1) & ~_SLEEP
    bus.write_byte_data(addr, _MODE1, awake)
    time.sleep(0.005)
    prescale = max(3, min(255, int(round(self.oscillator_hz / (4096.0 * self.frequency_hz))) - 1))
    bus.write_byte_data(addr, _MODE1, (awake & 0x7F) | _SLEEP)
    bus.write_byte_data(addr, _PRESCALE, prescale)
    bus.write_byte_data(addr, _MODE1, awake)
    time.sleep(0.005)
    bus.write_byte_data(addr, _MODE1, awake | _RESTART | _AUTO_INCREMENT)

  def _write_channel(self, channel: int, data: list[int] | None) -> None:
    """`data` is [on_l, on_h, off_l, off_h], or None for full off (limp)."""
    with self._lock:
      self.pulses[channel] = data
      if self._bus is not None:
        self._bus.write_i2c_block_data(self.address, _LED0_ON_L + 4 * channel, data or _FULL_OFF)

  def _write_servo(self, servo: Servo, deg: float, *, pulse_us: float | None = None) -> None:
    us = servo.pulse_us(deg) if pulse_us is None else pulse_us
    ticks = int(round(us * self.frequency_hz * 4096 / 1e6))
    self._write_channel(servo.channel, [0, 0, ticks & 0xFF, (ticks >> 8) & 0x0F])
    self._written[servo.name] = us

  def _relax_all(self) -> None:
    for servo in self.servos.values():
      self._write_channel(servo.channel, None)
      self._written[servo.name] = None

  @property
  def relaxed(self) -> bool:
    return all(us is None for us in self._written.values())

  # ------------------------------------------------------------------ control

  def on_message(self, msg: RuntimeMessage, ctx: NodeContext) -> Iterable[Output] | None:
    payload = msg.payload
    if "name" in payload and "deg" in payload:
      payload = {payload["name"]: payload["deg"]}
    moves: dict[str, float] = {}
    for name, value in payload.items():
      if name not in self.servos or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        self._rejected += 1
        ctx.fault(f"servo command rejected: {name}={value!r} (servos: {', '.join(self.servos)})", kind="bad_command")
        return None
      moves[name] = self.servos[name].clamp(float(value))
    if ctx.estop_engaged:
      return None
    self.target.update(moves)
    return None

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    now = ctx.now_ns
    dt = 0.0 if self._last_ns is None else (now - self._last_ns) / 1e9
    self._last_ns = now
    self._ticks += 1
    if not ctx.estop_engaged:
      for name, servo in self.servos.items():
        current, goal = self.position[name], self.target[name]
        step = servo.max_speed_dps * dt
        moved = goal if abs(goal - current) <= step else current + math.copysign(step, goal - current)
        # A relaxed servo stays limp until a command moves it.
        if moved != current:
          self.position[name] = moved
          self._write_servo(servo, moved)
    if self._ticks % self.state_every_n_ticks == 0:
      return [(self.state_topic, "ServoState", self._state(ctx))]
    return None

  def _state(self, ctx: NodeContext) -> dict[str, Any]:
    return {
      "servos": {
        name: {
          "deg": round(self.position[name], 2),
          "target_deg": round(self.target[name], 2),
          "us": None if self._written[name] is None else round(self._written[name], 1),
        }
        for name in self.servos
      },
      "relaxed": self.relaxed,
      "estop": ctx.estop_engaged,
    }

  def safe_stop(self, ctx: NodeContext) -> None:
    # Hold: aim where the servo is now, so it stops moving and stays put after a clear.
    self.target = dict(self.position)
    if self.on_stop == "relax":
      self._relax_all()

  def hard_stop(self) -> None:
    if self.on_stop == "relax":
      try:
        self._relax_all()
      except OSError:  # pragma: no cover - hardware specific
        pass

  def teardown(self, ctx: NodeContext) -> None:
    if self.on_stop == "relax":
      self._relax_all()
    if self._bus is not None:
      with self._lock:
        self._bus.close()
        self._bus = None

  def status(self) -> dict[str, Any]:
    return {
      "backend": self.resolved_backend or self.backend_name,
      "address": f"0x{self.address:02x}",
      "positions": {name: round(deg, 2) for name, deg in self.position.items()},
      "relaxed": self.relaxed,
      "rejected_commands": self._rejected,
    }

  # ------------------------------------------------------------------ calibration

  def calibrate(self, action: str, args: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    """Wizard actions: describe, jog (drive one servo to a raw pulse width), set_limit
    (min/max/home from the current pulse), and home."""
    if action in ("jog", "set_limit", "home"):
      if ctx.estop_engaged:
        raise ValueError("clear the e-stop first; the servo will move")
      if action == "home":
        self.target = {name: s.home for name, s in self.servos.items()}
      else:
        servo = self.servos.get(str(args.get("servo")))
        if servo is None:
          raise ValueError(f"no servo named {args.get('servo')!r}; servos: {', '.join(self.servos)}")
        if action == "jog":
          self._jog(servo, args)
        else:
          self._set_limit(servo, str(args.get("which")))
    elif action != "describe":
      raise ValueError(f"unknown servo calibration action {action!r}")
    return {
      "kind": "servos",
      "estop": ctx.estop_engaged,
      "servos": [
        {
          "name": s.name,
          "channel": s.channel,
          "min_us": s.min_us,
          "max_us": s.max_us,
          "home_deg": round(s.home, 2),
          "min_deg": s.min_deg,
          "max_deg": s.max_deg,
          "us": None if self._written[s.name] is None else round(self._written[s.name], 1),
        }
        for s in self.servos.values()
      ],
    }

  def _jog(self, servo: Servo, args: dict[str, Any]) -> None:
    try:
      us = float(args["us"])
    except (KeyError, TypeError, ValueError):
      raise ValueError("jog needs us: the pulse width in microseconds") from None
    lo, hi = _PULSE_BOUNDS_US
    us = max(lo, min(hi, us))
    self._write_servo(servo, 0.0, pulse_us=us)
    deg = servo.clamp(servo.degrees(us))
    self.position[servo.name] = self.target[servo.name] = deg

  def _set_limit(self, servo: Servo, which: str) -> None:
    us = self._written[servo.name]
    if us is None:
      raise ValueError("jog the servo first")
    if which == "home":
      if not servo.min_us <= us <= servo.max_us:
        raise ValueError("home must be inside the min and max limits")
      servo.home_deg = round(servo.clamp(servo.degrees(us)), 2)
    elif which in ("min", "max"):
      lo, hi = (us, servo.max_us) if which == "min" else (servo.min_us, us)
      if servo.invert:
        lo, hi = (servo.min_us, us) if which == "min" else (us, servo.max_us)
      trial = Servo(servo.name, servo.channel, lo, hi, servo.min_deg, servo.max_deg, None, servo.max_speed_dps, servo.invert)
      trial.check()
      servo.min_us, servo.max_us = round(lo, 1), round(hi, 1)
    else:
      raise ValueError("which must be min, max, or home")
    self._calibrated.add(servo.name)

  def calibration(self) -> dict[str, Any] | None:
    items = [
      {"name": s.name, "min_us": s.min_us, "max_us": s.max_us, **({"home_deg": s.home_deg} if s.home_deg is not None else {})}
      for s in self.servos.values()
      if s.name in self._calibrated
    ]
    return {"servos": items} if items else None
