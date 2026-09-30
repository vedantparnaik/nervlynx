"""`nervlynx doctor`: plain-English checks for the setup problems that stop robots working.

Every probe goes through `System`, so the checks can run against a simulated machine
(a Pi 5 with a sagging supply, a laptop, a Zero 2 W without I2C) in tests.
"""

from __future__ import annotations

import glob
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from robot_core.hardware import BoardInfo, HardwareUnavailable, _importable, resolve_backend

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
_TAGS = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]", INFO: "[info]"}
_VENV_HINT = "python3 -m venv --system-site-packages .venv (then reinstall NervLynx into it)"
_APT_HINT = "(a virtualenv needs --system-site-packages to see apt packages)"
_PSU_FIX = (
  "Use the official supply for your board (5 V 5 A for a Pi 5, 5 V 3 A for a Pi 4, 5 V 2.5 A for a Zero 2 W) "
  "and power the motors from their own battery or regulator."
)
_COOLING_FIX = "Add a heatsink or fan, and keep the Pi out of enclosed spaces while motors run."
_OPTIONAL_MODULES = ("gpiozero", "lgpio", "RPi.GPIO", "picamera2", "cv2", "smbus2", "serial", "zmq", "zenoh")


@dataclass(frozen=True)
class Check:
  name: str
  status: str
  summary: str
  fix: str | None = None

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


class System:
  """Read-only view of the machine doctor runs on. Tests substitute a fake."""

  def read_text(self, path: str) -> str | None:
    try:
      return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
      return None

  def exists(self, path: str) -> bool:
    return os.path.exists(path)

  def accessible(self, path: str) -> bool:
    return os.access(path, os.R_OK | os.W_OK)

  def glob(self, pattern: str) -> list[str]:
    return sorted(glob.glob(pattern))

  def run(self, argv: list[str], timeout_s: float = 5.0) -> str | None:
    """Combined stdout/stderr, or None when the tool is missing or fails to start."""
    if shutil.which(argv[0]) is None:
      return None
    try:
      done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError):
      return None
    return (done.stdout or "") + (done.stderr or "")

  def importable(self, module: str) -> bool:
    return _importable(module)

  def groups(self) -> set[str]:
    try:
      import grp
    except ImportError:
      return set()
    names: set[str] = set()
    for gid in os.getgroups():
      try:
        names.add(grp.getgrgid(gid).gr_name)
      except KeyError:
        continue
    return names

  def python_version(self) -> tuple[int, int, int]:
    return tuple(sys.version_info[:3])  # type: ignore[return-value]

  def venv(self) -> tuple[bool, bool]:
    """(running inside a virtualenv, that virtualenv can see system site-packages)."""
    if sys.prefix == sys.base_prefix:
      return False, True
    cfg = self.read_text(os.path.join(sys.prefix, "pyvenv.cfg")) or ""
    for line in cfg.splitlines():
      key, _, value = line.partition("=")
      if key.strip() == "include-system-site-packages":
        return True, value.strip().lower() == "true"
    return True, False

  def disk_free_bytes(self, path: str) -> int | None:
    try:
      return shutil.disk_usage(path).free
    except OSError:
      return None

  def hostname(self) -> str:
    return platform.node() or "localhost"

  def platform_label(self) -> str:
    return f"{platform.system()} {platform.machine()}".strip()


def board_from(system: System) -> BoardInfo:
  raw = system.read_text("/proc/device-tree/model") or ""
  model = raw.split("\x00", 1)[0].strip()
  return BoardInfo(model=model or None)


def _is_linux(system: System) -> bool:
  return system.platform_label().startswith("Linux")


def check_python(system: System, board: BoardInfo) -> Check:
  version = ".".join(str(part) for part in system.python_version())
  if system.python_version() < (3, 10):
    return Check("python", FAIL, f"Python {version} is too old", "Install Python 3.10 or newer.")
  in_venv, sees_system = system.venv()
  if not in_venv:
    return Check("python", OK, f"Python {version} (system interpreter)")
  if board.is_raspberry_pi and not sees_system:
    return Check(
      "python",
      WARN,
      f"Python {version} in a virtualenv that can't see apt packages such as gpiozero and picamera2",
      _VENV_HINT,
    )
  return Check("python", OK, f"Python {version} in a virtualenv")


def check_board(system: System, board: BoardInfo) -> Check:
  if board.is_raspberry_pi:
    return Check("board", OK, board.model or "Raspberry Pi")
  if board.model and "Jetson" in board.model:
    return Check("board", INFO, f"{board.model}: GPIO backends are not supported yet, so hardware nodes use mock pins")
  label = board.model or system.platform_label()
  return Check("board", INFO, f"{label}: not a Raspberry Pi, so hardware nodes use mock pins and simulations run as-is")


def check_gpio(system: System, board: BoardInfo) -> Check:
  try:
    backend = resolve_backend("auto", board=board, importable=system.importable)
  except HardwareUnavailable as exc:
    return Check("gpio", FAIL, str(exc).split(":", 1)[0], f"sudo apt install python3-gpiozero python3-lgpio {_APT_HINT}")
  if backend == "mock":
    return Check("gpio", INFO, "backend auto -> mock (no GPIO on this machine)")
  if backend == "gpiozero" and not system.importable("lgpio"):
    if board.has_rp1:
      return Check("gpio", FAIL, "gpiozero is installed but lgpio isn't, so it can't reach the Pi 5's pins", f"sudo apt install python3-lgpio {_APT_HINT}")
    if not system.importable("RPi.GPIO"):
      return Check("gpio", WARN, "gpiozero has no pin library to drive the pins with", f"sudo apt install python3-lgpio {_APT_HINT}")
  device_glob = "/dev/gpiochip*" if backend == "gpiozero" else "/dev/gpiomem*"
  devices = system.glob(device_glob)
  if not devices:
    return Check("gpio", FAIL, f"backend auto -> {backend}, but no {device_glob} device exists", "Update Raspberry Pi OS (sudo apt full-upgrade) and reboot.")
  if not any(system.accessible(dev) for dev in devices):
    return Check("gpio", FAIL, f"backend auto -> {backend}, but you don't have permission to use the GPIO pins", "sudo usermod -aG gpio $USER, then log out and back in.")
  return Check("gpio", OK, f"backend auto -> {backend}, pins accessible")


def check_i2c(system: System) -> Check:
  bus = "/dev/i2c-1"
  if not system.exists(bus):
    return Check("i2c", WARN, "I2C is off (needed for IMUs, ToF distance sensors, and servo boards)", "sudo raspi-config nonint do_i2c 0, then reboot.")
  if not system.accessible(bus):
    return Check("i2c", FAIL, "I2C is on, but you don't have permission to use it", "sudo usermod -aG i2c $USER, then log out and back in.")
  if not system.importable("smbus2"):
    return Check("i2c", WARN, "I2C is on, but Python can't talk to it yet", "pip install smbus2 (or sudo apt install python3-smbus2).")
  return Check("i2c", OK, "I2C bus 1 is on")


def check_spi(system: System) -> Check:
  if system.exists("/dev/spidev0.0"):
    return Check("spi", OK, "SPI is on")
  return Check("spi", INFO, "SPI is off (only needed for SPI sensors and displays)", "sudo raspi-config nonint do_spi 0, then reboot.")


def _pi_cameras(system: System) -> list[str] | None:
  for tool in ("rpicam-hello", "libcamera-hello"):
    out = system.run([tool, "--list-cameras"])
    if out is not None:
      return re.findall(r"^\s*\d+\s*:\s*(\S+)", out, flags=re.MULTILINE)
  return None


def check_camera(system: System, board: BoardInfo) -> Check | None:
  usb = [Path(p).name for p in system.glob("/dev/v4l/by-id/*-video-index0")]
  csi = _pi_cameras(system) if board.is_raspberry_pi else None
  if csi:
    if not system.importable("picamera2"):
      return Check("camera", WARN, f"Pi camera {', '.join(csi)} found, but picamera2 isn't installed", f"sudo apt install python3-picamera2 {_APT_HINT}")
    return Check("camera", OK, f"Pi camera: {', '.join(csi)}" + (f"; USB: {len(usb)}" if usb else ""))
  if usb:
    if not system.importable("cv2"):
      return Check("camera", WARN, f"USB camera found ({usb[0]}), but OpenCV isn't installed", f"sudo apt install python3-opencv {_APT_HINT}")
    return Check("camera", OK, f"USB camera: {', '.join(usb)}")
  if board.is_raspberry_pi and csi is None:
    return Check("camera", INFO, "camera tools (rpicam-apps) not installed, so the Pi camera check was skipped", "sudo apt install rpicam-apps")
  if board.is_raspberry_pi or _is_linux(system):
    return Check("camera", INFO, "no camera detected")
  return None


def check_serial(system: System) -> Check | None:
  if not _is_linux(system):
    return None
  named = [Path(p).name for p in system.glob("/dev/serial/by-id/*")]
  raw = system.glob("/dev/ttyUSB*") + system.glob("/dev/ttyACM*")
  if not named and not raw:
    return Check("serial", INFO, "no USB serial devices (LiDAR, GPS, ESP32) connected")
  listing = ", ".join(named or [Path(p).name for p in raw])
  if "dialout" not in system.groups():
    return Check("serial", WARN, f"USB serial devices found ({listing}), but you're not in the dialout group", "sudo usermod -aG dialout $USER, then log out and back in.")
  return Check("serial", OK, f"USB serial: {listing}")


def check_power(system: System) -> Check:
  out = system.run(["vcgencmd", "get_throttled"])
  match = re.search(r"throttled=(0x[0-9a-fA-F]+)", out or "")
  if match is None:
    return Check("power", INFO, "vcgencmd isn't available, so under-voltage couldn't be checked")
  bits = int(match.group(1), 16)
  if bits & 0x1:
    return Check("power", FAIL, "Under-voltage right now: the supply is sagging, which causes random reboots and SD card corruption", _PSU_FIX)
  if bits & 0x10000:
    return Check("power", WARN, "Under-voltage happened since boot (typical when motors share the Pi's supply)", _PSU_FIX)
  if bits & 0xE:
    return Check("power", WARN, "The CPU is being throttled right now", _COOLING_FIX)
  if bits & 0xE0000:
    return Check("power", WARN, "The CPU was throttled since boot", _COOLING_FIX)
  return Check("power", OK, "no under-voltage or throttling since boot")


def check_temperature(system: System) -> Check | None:
  raw = system.read_text("/sys/class/thermal/thermal_zone0/temp")
  try:
    celsius = int((raw or "").strip()) / 1000.0
  except ValueError:
    return None
  if celsius >= 80.0:
    return Check("temperature", FAIL, f"CPU at {celsius:.1f} °C: the Pi slows itself down above 80 °C", _COOLING_FIX)
  if celsius >= 70.0:
    return Check("temperature", WARN, f"CPU at {celsius:.1f} °C, close to the 80 °C throttling point", _COOLING_FIX)
  return Check("temperature", OK, f"CPU at {celsius:.1f} °C")


def check_memory(system: System) -> Check | None:
  raw = system.read_text("/proc/meminfo")
  if not raw:
    return None
  values: dict[str, int] = {}
  for line in raw.splitlines():
    match = re.match(r"(\w+):\s+(\d+)\s*kB", line)
    if match:
      values[match.group(1)] = int(match.group(2)) // 1024
  total, available = values.get("MemTotal"), values.get("MemAvailable")
  if total is None or available is None:
    return None
  if available < 150:
    return Check("memory", WARN, f"only {available} MB of {total} MB RAM free", "Close other programs, or lower camera resolution and frame rate in your config.")
  return Check("memory", OK, f"{available} MB of {total} MB RAM free")


def check_disk(system: System) -> Check | None:
  free = system.disk_free_bytes(".")
  if free is None:
    return None
  gib = free / 2**30
  if gib < 1.0:
    return Check("disk", WARN, f"only {gib:.1f} GB free; run traces and camera recordings fill this quickly", "Delete old runs under logs/live/ or use a bigger SD card.")
  return Check("disk", OK, f"{gib:.1f} GB free")


def check_packages(system: System) -> Check:
  present = [m for m in _OPTIONAL_MODULES if system.importable(m)]
  return Check("packages", INFO, "optional packages available: " + (", ".join(present) if present else "none"))


def check_dashboard(system: System) -> Check:
  host = system.hostname()
  return Check("dashboard", INFO, f"from another device: http://{host}.local:9120 (start run-live with --host 0.0.0.0)")


def check_config(path: Path) -> Check:
  from robot_core.live_config import validate_live_config
  from robot_core.project import load_project

  try:
    cfg, registry, problems = load_project(path)
  except Exception as exc:  # noqa: BLE001 - any load failure is reported the same way
    return Check("config", FAIL, f"{path} can't be read: {exc}")
  issues = problems + validate_live_config(cfg, registry)
  if issues:
    more = f" (+{len(issues) - 3} more)" if len(issues) > 3 else ""
    return Check("config", FAIL, f"{path}: " + "; ".join(issues[:3]) + more, f"nervlynx validate {path} lists every problem.")
  return Check("config", OK, f"{path} is valid ({len(cfg['nodes'])} nodes)")


def run_checks(system: System | None = None, *, config: Path | None = None) -> list[Check]:
  system = system or System()
  board = board_from(system)
  checks: list[Check | None] = [check_python(system, board), check_board(system, board), check_gpio(system, board)]
  if board.is_raspberry_pi:
    checks += [check_i2c(system), check_spi(system)]
  checks += [check_camera(system, board), check_serial(system)]
  if board.is_raspberry_pi:
    checks += [check_power(system), check_temperature(system)]
  checks += [check_memory(system), check_disk(system), check_packages(system)]
  if board.is_raspberry_pi:
    checks.append(check_dashboard(system))
  if config is not None:
    checks.append(check_config(config))
  return [check for check in checks if check is not None]


def render_text(checks: list[Check]) -> str:
  width = max(len(check.name) for check in checks)
  lines = ["NervLynx doctor"]
  for check in checks:
    lines.append(f"  {_TAGS[check.status]} {check.name:<{width}}  {check.summary}")
    if check.fix:
      lines.append(f"  {'':6} {'':<{width}}  fix: {check.fix}")
  fails = sum(check.status == FAIL for check in checks)
  warns = sum(check.status == WARN for check in checks)
  if not fails and not warns:
    lines.append("all good")
  else:
    parts = [f"{fails} problem{'s' if fails != 1 else ''} to fix"] if fails else []
    parts += [f"{warns} warning{'s' if warns != 1 else ''}"] if warns else []
    lines.append(", ".join(parts))
  return "\n".join(lines)
