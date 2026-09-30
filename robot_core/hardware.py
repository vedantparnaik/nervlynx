"""GPIO/PWM backends and DC motor drivers.

Backends:
  mock      in-memory pins; the default everywhere and what tests and simulations use
  rpi_gpio  RPi.GPIO software PWM (Raspberry Pi 4 and older)
  gpiozero  gpiozero PWMOutputDevice / DigitalOutputDevice (any Raspberry Pi, lgpio on Pi 5)
  auto      gpiozero on a Raspberry Pi (RPi.GPIO if gpiozero is missing and the board
            supports it), mock on any other machine, so one config runs in simulation
            and on the robot

Pins are BCM numbers. Duty cycles are 0..1 and motor speeds are -1..1. Every backend call
is serialised by a lock so `hard_stop()` can run from another thread.
"""

from __future__ import annotations

import importlib.util
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

BACKENDS = ("mock", "rpi_gpio", "gpiozero", "auto")
DRIVERS = ("bts7960", "tb6612", "l298n")
DEVICE_TREE_MODEL = Path("/proc/device-tree/model")
_RP1_MODEL = re.compile(r"Raspberry Pi (?:5|500)\b|Compute Module 5\b")
_GPIO_INSTALL_HINT = "sudo apt install python3-gpiozero python3-lgpio (a virtualenv needs --system-site-packages to see them)"


class HardwareUnavailable(RuntimeError):
  pass


@dataclass(frozen=True)
class BoardInfo:
  model: str | None

  @property
  def is_raspberry_pi(self) -> bool:
    return bool(self.model) and self.model.startswith("Raspberry Pi")

  @property
  def has_rp1(self) -> bool:
    """Pi 5-family boards drive GPIO through the RP1 chip, which RPi.GPIO cannot access."""
    return bool(self.model) and bool(_RP1_MODEL.search(self.model))


def detect_board(model_path: Path = DEVICE_TREE_MODEL) -> BoardInfo:
  try:
    raw = model_path.read_bytes()
  except OSError:
    return BoardInfo(model=None)
  model = raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
  return BoardInfo(model=model or None)


def _importable(module: str) -> bool:
  try:
    return importlib.util.find_spec(module) is not None
  except (ImportError, ValueError):
    return False


def resolve_backend(
  name: str,
  *,
  board: BoardInfo | None = None,
  importable: Callable[[str], bool] = _importable,
) -> str:
  """Turn `auto` into a concrete backend for this machine; other names pass through."""
  if name != "auto":
    return name
  board = board if board is not None else detect_board()
  if not board.is_raspberry_pi:
    return "mock"
  if importable("gpiozero"):
    return "gpiozero"
  if board.has_rp1:
    raise HardwareUnavailable(f"{board.model} needs gpiozero with lgpio (RPi.GPIO does not support it): {_GPIO_INSTALL_HINT}")
  if importable("RPi.GPIO"):
    return "rpi_gpio"
  raise HardwareUnavailable(f"no GPIO library found on {board.model}: {_GPIO_INSTALL_HINT}")


class PinBackend:
  name = "base"

  def setup_pwm(self, pin: int, frequency_hz: float) -> None:
    raise NotImplementedError

  def write_pwm(self, pin: int, duty: float) -> None:
    raise NotImplementedError

  def setup_digital(self, pin: int, initial: bool = False) -> None:
    raise NotImplementedError

  def write_digital(self, pin: int, value: bool) -> None:
    raise NotImplementedError

  def close(self) -> None:
    raise NotImplementedError


class MockBackend(PinBackend):
  """Records pin state in memory. Safe on any machine."""

  name = "mock"

  def __init__(self) -> None:
    self._lock = threading.Lock()
    self.pwm: dict[int, float] = {}
    self.frequency_hz: dict[int, float] = {}
    self.digital: dict[int, bool] = {}
    self.writes = 0
    self.closed = False

  def setup_pwm(self, pin: int, frequency_hz: float) -> None:
    with self._lock:
      self.pwm[pin] = 0.0
      self.frequency_hz[pin] = frequency_hz

  def write_pwm(self, pin: int, duty: float) -> None:
    with self._lock:
      if pin not in self.pwm:
        raise KeyError(f"pwm pin {pin} was not set up")
      self.pwm[pin] = duty
      self.writes += 1

  def setup_digital(self, pin: int, initial: bool = False) -> None:
    with self._lock:
      self.digital[pin] = initial

  def write_digital(self, pin: int, value: bool) -> None:
    with self._lock:
      if pin not in self.digital:
        raise KeyError(f"digital pin {pin} was not set up")
      self.digital[pin] = value
      self.writes += 1

  def close(self) -> None:
    with self._lock:
      for pin in self.pwm:
        self.pwm[pin] = 0.0
      for pin in self.digital:
        self.digital[pin] = False
      self.closed = True


class RPiGpioBackend(PinBackend):
  """RPi.GPIO software PWM, matching the rover's original teleop server."""

  name = "rpi_gpio"

  def __init__(self) -> None:
    try:
      import RPi.GPIO as GPIO  # type: ignore[import-not-found]
    except ImportError as exc:
      raise HardwareUnavailable("RPi.GPIO is not installed; run on a Raspberry Pi or use backend: mock") from exc
    self._gpio = GPIO
    self._lock = threading.Lock()
    self._pwm: dict[int, Any] = {}
    self._pins: set[int] = set()
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)

  def setup_pwm(self, pin: int, frequency_hz: float) -> None:
    with self._lock:
      self._gpio.setup(pin, self._gpio.OUT)
      channel = self._gpio.PWM(pin, frequency_hz)
      channel.start(0)
      self._pwm[pin] = channel
      self._pins.add(pin)

  def write_pwm(self, pin: int, duty: float) -> None:
    with self._lock:
      self._pwm[pin].ChangeDutyCycle(max(0.0, min(100.0, duty * 100.0)))

  def setup_digital(self, pin: int, initial: bool = False) -> None:
    with self._lock:
      if pin in self._pins:
        self._gpio.output(pin, self._gpio.HIGH if initial else self._gpio.LOW)
        return
      self._gpio.setup(pin, self._gpio.OUT, initial=self._gpio.HIGH if initial else self._gpio.LOW)
      self._pins.add(pin)

  def write_digital(self, pin: int, value: bool) -> None:
    with self._lock:
      self._gpio.output(pin, self._gpio.HIGH if value else self._gpio.LOW)

  def close(self) -> None:
    with self._lock:
      for channel in self._pwm.values():
        channel.ChangeDutyCycle(0)
        channel.stop()
      self._pwm.clear()
      if self._pins:
        self._gpio.cleanup(sorted(self._pins))
      self._pins.clear()


class GpiozeroBackend(PinBackend):
  name = "gpiozero"

  def __init__(self) -> None:
    try:
      from gpiozero import DigitalOutputDevice, PWMOutputDevice  # type: ignore[import-not-found]
    except ImportError as exc:
      raise HardwareUnavailable("gpiozero is not installed; run on a Raspberry Pi or use backend: mock") from exc
    self._pwm_cls = PWMOutputDevice
    self._digital_cls = DigitalOutputDevice
    self._lock = threading.Lock()
    self._pwm: dict[int, Any] = {}
    self._digital: dict[int, Any] = {}

  def setup_pwm(self, pin: int, frequency_hz: float) -> None:
    with self._lock:
      self._pwm[pin] = self._pwm_cls(pin, frequency=int(frequency_hz), initial_value=0.0)

  def write_pwm(self, pin: int, duty: float) -> None:
    with self._lock:
      self._pwm[pin].value = max(0.0, min(1.0, duty))

  def setup_digital(self, pin: int, initial: bool = False) -> None:
    with self._lock:
      device = self._digital.get(pin)
      if device is None:
        self._digital[pin] = self._digital_cls(pin, initial_value=initial)
      else:
        device.value = initial

  def write_digital(self, pin: int, value: bool) -> None:
    with self._lock:
      self._digital[pin].value = bool(value)

  def close(self) -> None:
    with self._lock:
      for device in list(self._pwm.values()) + list(self._digital.values()):
        device.value = 0
        device.close()
      self._pwm.clear()
      self._digital.clear()


def create_backend(name: str, *, board: BoardInfo | None = None) -> PinBackend:
  resolved = resolve_backend(name, board=board)
  if resolved == "mock":
    return MockBackend()
  if resolved == "rpi_gpio":
    return RPiGpioBackend()
  if resolved == "gpiozero":
    return GpiozeroBackend()
  raise ValueError(f"unknown hardware backend {name!r}; expected one of {', '.join(BACKENDS)}")


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
  return max(lo, min(hi, value))


class Motor:
  """A DC motor behind an H-bridge. `drive(speed)` takes -1..1 before inversion."""

  def __init__(self, name: str, invert: bool) -> None:
    self.name = name
    self.invert = invert
    self.command = 0.0
    self.duty = 0.0

  def drive(self, speed: float) -> None:
    raise NotImplementedError

  def stop(self) -> None:
    self.drive(0.0)

  def _bridge_speed(self, speed: float) -> float:
    self.command = _clamp(speed)
    # "+ 0.0" normalises -0.0 so an inverted motor at rest reports 0.0.
    return (-self.command if self.invert else self.command) + 0.0


class BTS7960Motor(Motor):
  """BTS7960 (IBT-2) half-bridge pair: RPWM drives forward, LPWM drives reverse."""

  def __init__(
    self,
    backend: PinBackend,
    name: str,
    rpwm: int,
    lpwm: int,
    *,
    invert: bool = False,
    frequency_hz: float = 1000.0,
    enable_pins: Iterable[int] = (),
  ) -> None:
    super().__init__(name, invert)
    self._backend = backend
    self.rpwm = rpwm
    self.lpwm = lpwm
    backend.setup_pwm(rpwm, frequency_hz)
    backend.setup_pwm(lpwm, frequency_hz)
    for pin in enable_pins:
      backend.setup_digital(pin, True)

  def drive(self, speed: float) -> None:
    s = self._bridge_speed(speed)
    # Release the opposite side first so both inputs are never driven at once.
    if s >= 0:
      self._backend.write_pwm(self.lpwm, 0.0)
      self._backend.write_pwm(self.rpwm, s)
    else:
      self._backend.write_pwm(self.rpwm, 0.0)
      self._backend.write_pwm(self.lpwm, -s)
    self.duty = s


class TB6612Motor(Motor):
  """One TB6612FNG channel: IN1/IN2 set direction, PWM sets speed, STBY enables the chip."""

  def __init__(
    self,
    backend: PinBackend,
    name: str,
    in1: int,
    in2: int,
    pwm: int,
    *,
    stby: int | None = None,
    invert: bool = False,
    frequency_hz: float = 1000.0,
  ) -> None:
    super().__init__(name, invert)
    self._backend = backend
    self.in1 = in1
    self.in2 = in2
    self.pwm = pwm
    backend.setup_digital(in1, False)
    backend.setup_digital(in2, False)
    backend.setup_pwm(pwm, frequency_hz)
    if stby is not None:
      backend.setup_digital(stby, True)

  def drive(self, speed: float) -> None:
    s = self._bridge_speed(speed)
    if s > 0:
      self._backend.write_digital(self.in2, False)
      self._backend.write_digital(self.in1, True)
      self._backend.write_pwm(self.pwm, s)
    elif s < 0:
      self._backend.write_digital(self.in1, False)
      self._backend.write_digital(self.in2, True)
      self._backend.write_pwm(self.pwm, -s)
    else:
      self._backend.write_pwm(self.pwm, 0.0)
      self._backend.write_digital(self.in1, False)
      self._backend.write_digital(self.in2, False)
    self.duty = s


class L298NMotor(Motor):
  """One L298N channel. With `en`, IN1/IN2 set direction and EN carries PWM (jumper removed);
  without it (EN jumpered high), IN1/IN2 carry PWM themselves, like a BTS7960."""

  def __init__(
    self,
    backend: PinBackend,
    name: str,
    in1: int,
    in2: int,
    *,
    en: int | None = None,
    invert: bool = False,
    frequency_hz: float = 1000.0,
  ) -> None:
    super().__init__(name, invert)
    self._backend = backend
    self.in1 = in1
    self.in2 = in2
    self.en = en
    if en is None:
      backend.setup_pwm(in1, frequency_hz)
      backend.setup_pwm(in2, frequency_hz)
    else:
      backend.setup_digital(in1, False)
      backend.setup_digital(in2, False)
      backend.setup_pwm(en, frequency_hz)

  def drive(self, speed: float) -> None:
    s = self._bridge_speed(speed)
    if self.en is None:
      forward, reverse = (self.in1, self.in2) if s >= 0 else (self.in2, self.in1)
      self._backend.write_pwm(reverse, 0.0)
      self._backend.write_pwm(forward, abs(s))
    elif s == 0:
      self._backend.write_pwm(self.en, 0.0)
      self._backend.write_digital(self.in1, False)
      self._backend.write_digital(self.in2, False)
    else:
      on, off = (self.in1, self.in2) if s > 0 else (self.in2, self.in1)
      self._backend.write_digital(off, False)
      self._backend.write_digital(on, True)
      self._backend.write_pwm(self.en, abs(s))
    self.duty = s


_REQUIRED_PINS = {"bts7960": ("rpwm", "lpwm"), "tb6612": ("in1", "in2", "pwm"), "l298n": ("in1", "in2")}
_OPTIONAL_PINS = {"bts7960": ("enable_pins",), "tb6612": ("stby",), "l298n": ("en",)}


def validate_motor_specs(driver: str, specs: Iterable[dict[str, Any]]) -> list[str]:
  """Check pin fields, types, and conflicts before any hardware is touched."""
  issues: list[str] = []
  if driver not in DRIVERS:
    return [f"unknown motor driver {driver!r}; expected one of {', '.join(DRIVERS)}"]
  allowed = {"name", "invert", *_REQUIRED_PINS[driver], *_OPTIONAL_PINS[driver]}
  owner: dict[int, str] = {}
  shared: set[int] = set()
  names: set[str] = set()
  for idx, spec in enumerate(specs):
    label = str(spec.get("name", f"motor[{idx}]")) if isinstance(spec, dict) else f"motor[{idx}]"
    if not isinstance(spec, dict):
      issues.append(f"{label} must be a mapping")
      continue
    if label in names:
      issues.append(f"duplicate motor name {label}")
    names.add(label)
    unknown = sorted(set(spec) - allowed)
    if unknown:
      issues.append(f"{label}: unknown fields for {driver}: {', '.join(unknown)}")
    if "invert" in spec and not isinstance(spec["invert"], bool):
      issues.append(f"{label}.invert must be true or false")
    pins: list[tuple[str, Any]] = [(field, spec.get(field)) for field in _REQUIRED_PINS[driver]]
    if driver == "l298n" and spec.get("en") is not None:
      pins.append(("en", spec["en"]))
    for field in _REQUIRED_PINS[driver]:
      if field not in spec:
        issues.append(f"{label}: missing required pin {field}")
    if driver == "tb6612" and spec.get("stby") is not None:
      stby = spec["stby"]
      if isinstance(stby, bool) or not isinstance(stby, int):
        issues.append(f"{label}.stby must be an integer BCM pin")
      else:
        shared.add(stby)
    if driver == "bts7960":
      enables = spec.get("enable_pins", [])
      if not isinstance(enables, list) or any(isinstance(p, bool) or not isinstance(p, int) for p in enables):
        issues.append(f"{label}.enable_pins must be a list of integer BCM pins")
      else:
        shared.update(enables)
    for field, pin in pins:
      if pin is None:
        continue
      if isinstance(pin, bool) or not isinstance(pin, int) or not 0 <= pin <= 27:
        issues.append(f"{label}.{field} must be a BCM pin number between 0 and 27")
        continue
      if pin in owner:
        issues.append(f"pin {pin} is used by both {owner[pin]} and {label}.{field}")
      owner[pin] = f"{label}.{field}"
  for pin in sorted(shared & set(owner)):
    issues.append(f"pin {pin} is both a shared enable/standby pin and {owner[pin]}")
  return issues


def build_motor(backend: PinBackend, driver: str, spec: dict[str, Any], *, frequency_hz: float = 1000.0) -> Motor:
  name = str(spec.get("name", "motor"))
  invert = bool(spec.get("invert", False))
  if driver == "bts7960":
    return BTS7960Motor(
      backend,
      name,
      int(spec["rpwm"]),
      int(spec["lpwm"]),
      invert=invert,
      frequency_hz=frequency_hz,
      enable_pins=[int(p) for p in spec.get("enable_pins", [])],
    )
  if driver == "tb6612":
    stby = spec.get("stby")
    return TB6612Motor(
      backend,
      name,
      int(spec["in1"]),
      int(spec["in2"]),
      int(spec["pwm"]),
      stby=int(stby) if stby is not None else None,
      invert=invert,
      frequency_hz=frequency_hz,
    )
  if driver == "l298n":
    en = spec.get("en")
    return L298NMotor(
      backend,
      name,
      int(spec["in1"]),
      int(spec["in2"]),
      en=int(en) if en is not None else None,
      invert=invert,
      frequency_hz=frequency_hz,
    )
  raise ValueError(f"unknown motor driver {driver!r}")
