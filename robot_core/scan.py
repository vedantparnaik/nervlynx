"""`nervlynx scan`: find attached sensors and suggest the nodes for them.

I2C devices are identified by reading their chip-ID registers rather than trusting the
address (0x68 can be an MPU6050, an ICM-20948, or a DS3231 clock). USB serial devices are
matched on vendor:product IDs from sysfs; nothing is written to them. Cameras are found
the same way `nervlynx doctor` finds them.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from robot_core.doctor import System, _pi_cameras, board_from

_EEPROM_RANGES = (range(0x30, 0x38), range(0x50, 0x60))


class I2CBus(Protocol):
  def read_byte(self, address: int) -> int: ...
  def write_quick(self, address: int) -> None: ...
  def read_byte_data(self, address: int, register: int) -> int: ...


@dataclass(frozen=True)
class Found:
  kind: str        # "i2c", "usb", "camera"
  where: str       # "0x68", "/dev/ttyUSB0", "imx708"
  name: str        # what it most likely is
  node: str | None = None       # NervLynx plugin that drives it, if any
  params: dict[str, Any] | None = None
  note: str | None = None

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


def _reg(bus: I2CBus, address: int, register: int) -> int | None:
  try:
    return bus.read_byte_data(address, register)
  except OSError:
    return None


def _identify_i2c(bus: I2CBus, address: int) -> Found:
  where = f"0x{address:02x}"
  if address in (0x68, 0x69):
    who = _reg(bus, address, 0x75)
    names = {0x68: "MPU6050", 0x70: "MPU6500", 0x71: "MPU9250", 0x73: "MPU9255"}
    if who in names:
      return Found("i2c", where, f"{names[who]} IMU", "mpu6050_imu", {"address": address})
    if _reg(bus, address, 0x00) == 0xEA:
      return Found("i2c", where, "ICM-20948 IMU", note="no built-in node yet")
    return Found("i2c", where, "unknown device (a DS3231 real-time clock often lives here)")
  if address in (0x28, 0x29) and _reg(bus, address, 0x00) == 0xA0:
    return Found("i2c", where, "BNO055 absolute orientation IMU", note="no built-in node yet")
  if address == 0x29 and _reg(bus, address, 0xC0) == 0xEE:
    return Found("i2c", where, "VL53L0X time-of-flight distance sensor", note="no built-in node yet")
  if address in (0x76, 0x77):
    chips = {0x60: "BME280 temperature/humidity/pressure", 0x58: "BMP280 temperature/pressure", 0x61: "BME680 environment"}
    chip = _reg(bus, address, 0xD0)
    if chip in chips:
      return Found("i2c", where, f"{chips[chip]} sensor", note="no built-in node yet")
  if address == 0x53 and _reg(bus, address, 0x00) == 0xE5:
    return Found("i2c", where, "ADXL345 accelerometer", note="no built-in node yet")
  if address == 0x40:
    mode1 = _reg(bus, address, 0x00)
    if mode1 is not None and (mode1 & 0xEF) in (0x01, 0x21, 0x00):
      return Found("i2c", where, "PCA9685 16-channel PWM/servo driver (likely)", note="no built-in node yet")
    return Found("i2c", where, "INA219 current sensor or PCA9685 servo driver")
  if address in (0x3C, 0x3D):
    return Found("i2c", where, "SSD1306 OLED display (likely)", note="no built-in node yet")
  if 0x48 <= address <= 0x4B:
    return Found("i2c", where, "ADS1115/ADS1015 ADC (likely)", note="no built-in node yet")
  if 0x20 <= address <= 0x27:
    return Found("i2c", where, "PCF8574/MCP23017 I/O expander (likely)", note="no built-in node yet")
  return Found("i2c", where, "unknown device")


def scan_i2c(bus: I2CBus) -> list[Found]:
  """Probe 0x03..0x77 like `i2cdetect -y` (reads for EEPROM ranges, quick writes elsewhere)."""
  found: list[Found] = []
  for address in range(0x03, 0x78):
    try:
      if any(address in r for r in _EEPROM_RANGES):
        bus.read_byte(address)
      else:
        bus.write_quick(address)
    except OSError:
      continue
    found.append(_identify_i2c(bus, address))
  return found


_USB_IDS: dict[str, tuple[str, str | None]] = {
  "10c4:ea60": ("CP210x USB-serial: an RPLidar, an ESP32 dev board, or another adapter", None),
  "1a86:7523": ("CH340 USB-serial: usually an Arduino or ESP32/ESP8266 clone", None),
  "1a86:55d4": ("CH9102 USB-serial: usually an ESP32 dev board", None),
  "0403:6001": ("FTDI FT232 USB-serial adapter", None),
  "067b:2303": ("PL2303 USB-serial: often a GPS module", None),
  "303a:1001": ("ESP32 with native USB (S2/S3/C3)", None),
  "2e8a:0005": ("Raspberry Pi Pico (MicroPython)", None),
  "2e8a:000a": ("Raspberry Pi Pico (C/C++ SDK serial)", None),
  "1546:01a7": ("u-blox 7 GPS", None),
  "1546:01a8": ("u-blox 8 GPS", None),
  "1546:01a9": ("u-blox 9 GPS", None),
}


def _usb_id(system: System, tty: str) -> str | None:
  device = f"/sys/class/tty/{tty}/device"
  for up in ("..", "../..", "../../.."):
    vendor = system.read_text(os.path.normpath(os.path.join(device, up, "idVendor")))
    product = system.read_text(os.path.normpath(os.path.join(device, up, "idProduct")))
    if vendor and product:
      return f"{vendor.strip()}:{product.strip()}"
  return None


def scan_usb_serial(system: System) -> list[Found]:
  found: list[Found] = []
  for path in system.glob("/dev/ttyUSB*") + system.glob("/dev/ttyACM*"):
    tty = Path(path).name
    usb_id = _usb_id(system, tty)
    name, node = _USB_IDS.get(usb_id or "", (f"USB serial device ({usb_id or 'unknown id'})", None))
    found.append(Found("usb", path, name, node, note=f"USB id {usb_id}" if usb_id else None))
  return found


def scan_cameras(system: System) -> list[Found]:
  found = [Found("camera", sensor, f"Pi camera {sensor}", "camera", {"name": "front", "optional": True}) for sensor in _pi_cameras(system) or []]
  for path in system.glob("/dev/v4l/by-id/*-video-index0"):
    found.append(Found("camera", path, "USB webcam", "camera", {"name": "usb" if found else "front", "source": "opencv", "optional": True}))
  return found


def _open_smbus(bus_number: int) -> Any:
  from smbus2 import SMBus  # type: ignore[import-not-found]

  return SMBus(bus_number)


def scan(
  system: System | None = None,
  *,
  bus_number: int = 1,
  open_bus: Callable[[int], Any] = _open_smbus,
) -> tuple[list[Found], list[str]]:
  """Everything found, plus notes about what could not be scanned and why."""
  system = system or System()
  notes: list[str] = []
  found: list[Found] = []
  device = f"/dev/i2c-{bus_number}"
  if not system.exists(device):
    if board_from(system).is_raspberry_pi:
      notes.append(f"I2C bus {bus_number} is off, so I2C sensors were not scanned: sudo raspi-config nonint do_i2c 0, then reboot")
  else:
    try:
      bus = open_bus(bus_number)
    except ImportError:
      notes.append("install smbus2 to scan I2C: pip install smbus2")
    except OSError as exc:
      notes.append(f"cannot open {device}: {exc} (are you in the i2c group?)")
    else:
      try:
        found += scan_i2c(bus)
      finally:
        getattr(bus, "close", lambda: None)()
  found += scan_usb_serial(system)
  found += scan_cameras(system)
  return found, notes


def draft_nodes_yaml(found: list[Found]) -> str:
  """YAML lines to paste under `nodes:` for what NervLynx can drive; the rest as comments."""
  lines = ["# Found by `nervlynx scan`. Paste the entries you want under `nodes:` in robot.yaml."]
  for item in found:
    if item.node:
      params = ", ".join(f"{k}: {_yaml_value(k, v)}" for k, v in (item.params or {}).items())
      lines.append(f"- plugin: {item.node}" + (f"\n  params: {{{params}}}" if params else "") + f"   # {item.name} at {item.where}")
    else:
      lines.append(f"# {item.name} at {item.where}" + (f" ({item.note})" if item.note else ""))
  return "\n".join(lines) + "\n"


def _yaml_value(key: str, value: Any) -> str:
  if key == "address" and isinstance(value, int):
    return f"0x{value:02x}"
  if isinstance(value, bool):
    return "true" if value else "false"
  return str(value)
