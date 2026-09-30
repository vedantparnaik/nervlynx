"""`gps_nmea`: position from any NMEA 0183 GPS (u-blox NEO-6M/7M/M8N/M9N and most others).

Sentences are read from a USB or UART serial port without blocking the executor (only
bytes already received), their checksums are checked, and GGA/RMC data is published on
`gps` whenever a new fix arrives:

  {"fix": true, "lat_deg": 51.4779, "lon_deg": -0.0015, "alt_m": 45.2, "sats": 9,
   "hdop": 0.9, "speed_mps": 0.4, "course_deg": 87.0, "quality": "gps", "utc": "12:35:19"}

Until the receiver has a fix (a cold start under open sky takes from 30 s to a few
minutes) `fix` is false and the position fields are None. The mock backend reports a
fix at `mock_origin`; in the simulator it follows `odom`, treating the sim world's x as
east and y as north, so waypoint code can be tried before the robot goes outside.
`distance_m` and `bearing_deg` help with waypoints.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable

from robot_core.hardware import HardwareUnavailable, resolve_backend
from robot_core.live import LiveNode, NodeContext, Output
from robot_core.runtime import RuntimeMessage

EARTH_RADIUS_M = 6_371_000.0
KNOTS_TO_MPS = 0.514444
_MAX_LINE = 256
_GPS_USB_IDS = ("1546:01a7", "1546:01a8", "1546:01a9", "067b:2303")
_QUALITY = {0: None, 1: "gps", 2: "dgps", 3: "pps", 4: "rtk", 5: "rtk_float", 6: "estimated", 7: "manual", 8: "simulated"}
_UART = "/dev/serial0"


def nmea_checksum(body: str) -> str:
  """Two hex digits: the XOR of every character between '$' and '*'."""
  value = 0
  for char in body:
    value ^= ord(char)
  return f"{value:02X}"


def parse_sentence(line: str) -> tuple[str, list[str]] | None:
  """("GGA", fields) for a well-formed sentence with a correct checksum, else None."""
  line = line.strip()
  if not line.startswith("$") or "*" not in line:
    return None
  body, _, checksum = line[1:].partition("*")
  if nmea_checksum(body) != checksum[:2].upper():
    return None
  fields = body.split(",")
  if len(fields[0]) < 5:
    return None
  return fields[0][-3:], fields[1:]


def _coordinate(value: str, hemisphere: str, degree_digits: int) -> float | None:
  if not value or hemisphere not in ("N", "S", "E", "W"):
    return None
  try:
    degrees = int(value[:degree_digits]) + float(value[degree_digits:]) / 60.0
  except ValueError:
    return None
  return -degrees if hemisphere in ("S", "W") else degrees


def _number(value: str) -> float | None:
  try:
    return float(value) if value else None
  except ValueError:
    return None


def _utc(value: str) -> str | None:
  return f"{value[0:2]}:{value[2:4]}:{value[4:6]}" if len(value) >= 6 and value[:6].isdigit() else None


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
  """Great-circle distance between two (lat, lon) points in degrees."""
  lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
  h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
  return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
  """Initial compass bearing from a to b: 0 is north, 90 is east."""
  lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
  y = math.sin(lon2 - lon1) * math.cos(lat2)
  x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
  return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def offset(origin: tuple[float, float], east_m: float, north_m: float) -> tuple[float, float]:
  """The point `east_m` east and `north_m` north of `origin` (flat-earth; fine for a few km)."""
  lat = origin[0] + math.degrees(north_m / EARTH_RADIUS_M)
  lon = origin[1] + math.degrees(east_m / (EARTH_RADIUS_M * math.cos(math.radians(origin[0]))))
  return lat, lon


class GpsState:
  """The receiver's latest report, updated sentence by sentence."""

  def __init__(self) -> None:
    self.fix = False
    self.lat_deg: float | None = None
    self.lon_deg: float | None = None
    self.alt_m: float | None = None
    self.sats: int | None = None
    self.hdop: float | None = None
    self.speed_mps: float | None = None
    self.course_deg: float | None = None
    self.quality: str | None = None
    self.utc: str | None = None

  def update(self, kind: str, fields: list[str]) -> bool:
    """Apply one parsed sentence; True if it was one this state uses."""
    if kind == "GGA" and len(fields) >= 9:
      try:
        quality = int(fields[5] or 0)
      except ValueError:
        quality = 0
      self.utc = _utc(fields[0]) or self.utc
      self.quality = _QUALITY.get(quality)
      self.fix = self.quality is not None
      self.sats = int(fields[6]) if fields[6].isdigit() else None
      self.hdop = _number(fields[7])
      self.alt_m = _number(fields[8]) if self.fix else None
      if self.fix:
        self.lat_deg = _coordinate(fields[1], fields[2], 2)
        self.lon_deg = _coordinate(fields[3], fields[4], 3)
        self.fix = self.lat_deg is not None and self.lon_deg is not None
      if not self.fix:
        self.lat_deg = self.lon_deg = self.alt_m = None
      return True
    if kind == "RMC" and len(fields) >= 8:
      self.utc = _utc(fields[0]) or self.utc
      if fields[1] == "A":
        knots = _number(fields[6])
        self.speed_mps = None if knots is None else knots * KNOTS_TO_MPS
        self.course_deg = _number(fields[7])
      else:
        self.speed_mps = self.course_deg = None
      return True
    return False

  def payload(self) -> dict[str, Any]:
    return {
      "fix": self.fix,
      "lat_deg": None if self.lat_deg is None else round(self.lat_deg, 7),
      "lon_deg": None if self.lon_deg is None else round(self.lon_deg, 7),
      "alt_m": self.alt_m,
      "sats": self.sats,
      "hdop": self.hdop,
      "speed_mps": None if self.speed_mps is None else round(self.speed_mps, 3),
      "course_deg": self.course_deg,
      "quality": self.quality,
      "utc": self.utc,
    }


def _open_serial(port: str, baud: int) -> Any:
  try:
    import serial  # type: ignore[import-not-found]
  except ImportError as exc:
    raise HardwareUnavailable("gps_nmea needs pyserial: pip install pyserial (or sudo apt install python3-serial)") from exc
  return serial.Serial(port, baud, timeout=0)


def find_gps_port(ports: Iterable[tuple[str, str | None]], uart_exists: bool) -> str:
  candidates = [path for path, usb_id in ports if usb_id in _GPS_USB_IDS]
  if len(candidates) == 1:
    return candidates[0]
  if len(candidates) > 1:
    raise HardwareUnavailable(f"several USB GPS receivers found ({', '.join(candidates)}); set port: to the right one")
  if uart_exists:
    return _UART
  raise HardwareUnavailable(
    "no GPS found: plug in a USB GPS, or wire a UART GPS to GPIO14/15 and enable the serial port "
    "(sudo raspi-config: Interface Options > Serial Port, login shell no, hardware yes), or set port:"
  )


def _detected_ports() -> tuple[list[tuple[str, str | None]], bool]:
  from robot_core.doctor import System
  from robot_core.scan import _usb_id

  system = System()
  ports = [(path, _usb_id(system, path.rsplit("/", 1)[-1])) for path in system.glob("/dev/ttyUSB*") + system.glob("/dev/ttyACM*")]
  return ports, system.exists(_UART)


class GpsNmea(LiveNode):
  """Publishes GPS fixes parsed from NMEA sentences."""

  rate_hz = 5.0

  def __init__(
    self,
    *,
    port: str = "auto",
    baud: int = 9600,
    topic: str = "gps",
    timeout_s: float = 3.0,
    mock_origin: list[float] | None = None,
    odom_topic: str = "odom",
    backend: str = "mock",
    open_serial: Callable[[str, int], Any] | None = None,
  ) -> None:
    if timeout_s <= 0:
      raise ValueError("timeout_s must be > 0")
    origin = mock_origin if mock_origin is not None else [51.4779, -0.0015]
    if (
      not isinstance(origin, (list, tuple))
      or len(origin) != 2
      or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in origin)
      or not (-90 <= origin[0] <= 90 and -180 <= origin[1] <= 180)
    ):
      raise ValueError("mock_origin must be [latitude, longitude] in degrees")
    self.port_name = port
    self.baud = int(baud)
    self.topic = topic
    self.timeout_ns = int(timeout_s * 1e9)
    self.origin = (float(origin[0]), float(origin[1]))
    self.odom_topic = odom_topic
    self.input_topics = (odom_topic,)
    self.backend_name = backend
    self.state = GpsState()
    self.sentences = 0
    self.bad_sentences = 0
    self._open_serial = open_serial or _open_serial
    self._port: Any = None
    self._mock = False
    self._buffer = bytearray()
    self._fresh = False
    self._last_rx_ns: int | None = None
    self._silent_reported = False

  def setup(self, ctx: NodeContext) -> None:
    resolved = resolve_backend(self.backend_name)
    if self.backend_name == "auto":
      ctx.fault(f"hardware backend auto resolved to {resolved}", severity="info", kind="hardware")
    if resolved == "mock":
      self._mock = True
      self.port_name = "simulated"
      self._set_mock_position(0.0, 0.0, 0.0, 90.0)
      return
    if self.port_name == "auto":
      self.port_name = find_gps_port(*_detected_ports())
    self._port = self._open_serial(self.port_name, self.baud)
    self._last_rx_ns = ctx.now_ns

  def _set_mock_position(self, east_m: float, north_m: float, speed_mps: float, course_deg: float) -> None:
    s = self.state
    s.lat_deg, s.lon_deg = offset(self.origin, east_m, north_m)
    s.fix, s.quality, s.sats, s.hdop, s.alt_m = True, "simulated", 10, 0.8, 0.0
    s.speed_mps, s.course_deg = speed_mps, course_deg
    self._fresh = True

  def on_message(self, msg: RuntimeMessage, ctx: NodeContext) -> Iterable[Output] | None:
    if not self._mock or msg.envelope.topic != self.odom_topic:
      return None
    p = msg.payload
    try:
      course = (90.0 - float(p.get("heading_deg", 0.0))) % 360.0
      self._set_mock_position(float(p["x_m"]), float(p["y_m"]), abs(float(p.get("speed_mps", 0.0))), round(course, 2))
    except (KeyError, TypeError, ValueError):
      return None
    return None

  def _read(self, now: int) -> None:
    waiting = self._port.in_waiting
    if waiting:
      self._buffer += self._port.read(waiting)
    while b"\n" in self._buffer:
      raw, _, rest = bytes(self._buffer).partition(b"\n")
      self._buffer = bytearray(rest)
      parsed = parse_sentence(raw.decode("ascii", "replace"))
      if parsed is None:
        self.bad_sentences += 1
        continue
      self.sentences += 1
      self._last_rx_ns = now
      if self.state.update(*parsed):
        self._fresh = True
    if len(self._buffer) > _MAX_LINE:
      self.bad_sentences += 1
      self._buffer.clear()

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    now = ctx.now_ns
    if self._port is not None:
      self._read(now)
      silent = now - (self._last_rx_ns if self._last_rx_ns is not None else now)
      if silent > self.timeout_ns and not self._silent_reported:
        self._silent_reported = True
        ctx.fault(
          f"gps: no NMEA data on {self.port_name} for {silent / 1e9:.1f} s "
          "(GPS TX goes to the Pi's RX pin, GPIO15; check power, and the baud rate: usually 9600)",
          kind="gps",
          severity="error",
        )
      elif silent <= self.timeout_ns:
        self._silent_reported = False
    if not self._fresh:
      return None
    self._fresh = False
    return [(self.topic, "GpsFix", self.state.payload())]

  def teardown(self, ctx: NodeContext) -> None:
    if self._port is not None:
      self._port.close()
      self._port = None

  def status(self) -> dict[str, Any]:
    return {
      "port": self.port_name,
      "fix": self.state.fix,
      "sats": self.state.sats,
      "quality": self.state.quality,
      "sentences": self.sentences,
      "bad_sentences": self.bad_sentences,
    }
