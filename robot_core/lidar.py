"""`lidar`: 2D scans from an LDROBOT LD19/LD06 or a Slamtec RPLidar (A1, A2, A3, C1, S1).

A reader thread parses the serial stream into whole revolutions, so the executor never
waits on the sensor; each tick publishes the newest complete scan on `scan`:

  {"ranges_m": [0.82, None, ...], "angle_min_deg": 0.0, "angle_increment_deg": 1.0,
   "range_min_m": 0.05, "range_max_m": 12.0, "nearest": {"distance_m": 0.42, "angle_deg": 348.0},
   "points": 452, "scan_hz": 10.0, "model": "ld19"}

`ranges_m[i]` is the closest return in the bin at `i * angle_increment_deg`, measured
counter-clockwise from the robot's front (ROS REP-103: 90 is left), or None when nothing
returned. Keeping the closest point per bin is the safe choice for avoiding obstacles.
`sector_min(scan, center_deg, width_deg)` answers "how close is the nearest thing ahead?".
The simulator publishes the same payload from its world (`skid_steer_sim` `lidar:`).
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Any, Callable, Iterable

from robot_core.hardware import HardwareUnavailable, resolve_backend
from robot_core.live import LiveNode, NodeContext, Output

MODELS = ("ld19", "ld06", "rplidar")
DEFAULT_BAUD = {"ld19": 230400, "ld06": 230400, "rplidar": 115200}
_LIDAR_USB_IDS = ("10c4:ea60",)

# LDROBOT LD06/LD19: 47-byte packets of 12 points, CRC-8 (polynomial 0x4D).
_LD_HEADER, _LD_VERLEN, _LD_PACKET, _LD_POINTS = 0x54, 0x2C, 47, 12


def _crc8_table(poly: int) -> list[int]:
  table = []
  for byte in range(256):
    crc = byte
    for _ in range(8):
      crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    table.append(crc)
  return table


_LD_CRC = _crc8_table(0x4D)


def ld_crc8(data: bytes) -> int:
  crc = 0
  for byte in data:
    crc = _LD_CRC[(crc ^ byte) & 0xFF]
  return crc


class LdParser:
  """LD06/LD19 byte stream -> revolutions of (clockwise angle in degrees, distance in m)."""

  def __init__(self) -> None:
    self._buffer = bytearray()
    self._points: list[tuple[float, float]] = []
    self._last_angle: float | None = None
    self._wrapped = False
    self.bad_packets = 0

  def feed(self, data: bytes) -> list[list[tuple[float, float]]]:
    self._buffer += data
    scans: list[list[tuple[float, float]]] = []
    buf = self._buffer
    while True:
      start = buf.find(bytes((_LD_HEADER, _LD_VERLEN)))
      if start < 0:
        del buf[: max(0, len(buf) - 1)]
        break
      if len(buf) - start < _LD_PACKET:
        del buf[:start]
        break
      packet = bytes(buf[start : start + _LD_PACKET])
      if ld_crc8(packet[:-1]) != packet[-1]:
        self.bad_packets += 1
        del buf[: start + 1]
        continue
      del buf[: start + _LD_PACKET]
      start_angle = int.from_bytes(packet[4:6], "little")
      end_angle = int.from_bytes(packet[42:44], "little")
      span = (end_angle - start_angle) % 36000
      step = span / (_LD_POINTS - 1)
      for i in range(_LD_POINTS):
        offset = 6 + 3 * i
        distance_mm = int.from_bytes(packet[offset : offset + 2], "little")
        angle = ((start_angle + step * i) % 36000) / 100.0
        if self._last_angle is not None and angle < self._last_angle - 180.0:
          if self._wrapped:  # the first revolution started mid-turn
            scans.append(self._points)
          self._wrapped = True
          self._points = []
        self._last_angle = angle
        if distance_mm:
          self._points.append((angle, distance_mm / 1000.0))
    return scans


def ld_packet(start_deg: float, end_deg: float, distances_mm: list[int], speed_dps: int = 3600, stamp_ms: int = 0) -> bytes:
  """Build a valid LD06/LD19 packet (the simulator and tests use it)."""
  if len(distances_mm) != _LD_POINTS:
    raise ValueError(f"an LD packet has {_LD_POINTS} points")
  body = bytearray((_LD_HEADER, _LD_VERLEN))
  body += int(speed_dps).to_bytes(2, "little") + int(round(start_deg * 100) % 36000).to_bytes(2, "little")
  for distance in distances_mm:
    body += int(distance).to_bytes(2, "little") + bytes((200,))
  body += int(round(end_deg * 100) % 36000).to_bytes(2, "little") + int(stamp_ms % 30000).to_bytes(2, "little")
  return bytes(body) + bytes((ld_crc8(bytes(body)),))


# Slamtec RPLidar standard scan: a 7-byte response descriptor, then 5-byte samples.
_RP_SYNC = 0xA5
_RP_STOP, _RP_SCAN, _RP_MOTOR_PWM = 0x25, 0x20, 0xF0
_RP_SCAN_DESCRIPTOR = bytes((0xA5, 0x5A, 0x05, 0x00, 0x00, 0x40, 0x81))


def rp_command(cmd: int, payload: bytes = b"") -> bytes:
  if not payload:
    return bytes((_RP_SYNC, cmd))
  packet = bytes((_RP_SYNC, cmd, len(payload))) + payload
  checksum = 0
  for byte in packet:
    checksum ^= byte
  return packet + bytes((checksum,))


class RpParser:
  """RPLidar standard-scan byte stream -> revolutions of (clockwise degrees, metres)."""

  def __init__(self) -> None:
    self._buffer = bytearray()
    self._synced = False
    self._points: list[tuple[float, float]] = []
    self._started = False
    self.bad_samples = 0

  def feed(self, data: bytes) -> list[list[tuple[float, float]]]:
    self._buffer += data
    buf = self._buffer
    if not self._synced:
      at = buf.find(_RP_SCAN_DESCRIPTOR)
      if at < 0:
        del buf[: max(0, len(buf) - len(_RP_SCAN_DESCRIPTOR) + 1)]
        return []
      del buf[: at + len(_RP_SCAN_DESCRIPTOR)]
      self._synced = True
    scans: list[list[tuple[float, float]]] = []
    while len(buf) >= 5:
      b0, b1, b2, b3, b4 = buf[0], buf[1], buf[2], buf[3], buf[4]
      start, inverse = b0 & 1, (b0 >> 1) & 1
      if start == inverse or not b1 & 1:
        self.bad_samples += 1
        del buf[0]
        continue
      del buf[:5]
      if start:
        if self._started and self._points:
          scans.append(self._points)
        self._points = []
        self._started = True
      quality = b0 >> 2
      distance_mm = (b3 | (b4 << 8)) / 4.0
      if self._started and quality and distance_mm:
        self._points.append((((b1 >> 1) | (b2 << 7)) / 64.0, distance_mm / 1000.0))
    return scans


def rp_sample(angle_deg: float, distance_mm: float, *, start: bool = False, quality: int = 47) -> bytes:
  """Build one RPLidar standard-scan sample (tests use it)."""
  angle_q6 = int(round(angle_deg * 64)) & 0x7FFF
  distance_q2 = int(round(distance_mm * 4)) & 0xFFFF
  s = 1 if start else 0
  return bytes(((quality << 2) | ((1 - s) << 1) | s, ((angle_q6 & 0x7F) << 1) | 1, angle_q6 >> 7, distance_q2 & 0xFF, distance_q2 >> 8))


def bin_scan(
  points: Iterable[tuple[float, float]],
  *,
  bins: int,
  range_min_m: float,
  range_max_m: float,
  mount_deg: float = 0.0,
  upside_down: bool = False,
) -> list[float | None]:
  """Closest return per bin, with bins counter-clockwise from the robot's front."""
  step = 360.0 / bins
  ranges: list[float | None] = [None] * bins
  for angle_cw, distance in points:
    if not range_min_m <= distance <= range_max_m:
      continue
    angle = (angle_cw if upside_down else -angle_cw) + mount_deg
    index = int(round(angle / step)) % bins
    current = ranges[index]
    if current is None or distance < current:
      ranges[index] = round(distance, 3)
  return ranges


def scan_payload(ranges: list[float | None], *, range_min_m: float, range_max_m: float, points: int, scan_hz: float, model: str) -> dict[str, Any]:
  step = 360.0 / len(ranges)
  nearest = None
  for index, distance in enumerate(ranges):
    if distance is not None and (nearest is None or distance < nearest["distance_m"]):
      nearest = {"distance_m": distance, "angle_deg": round(index * step, 3)}
  return {
    "ranges_m": ranges,
    "angle_min_deg": 0.0,
    "angle_increment_deg": round(step, 6),
    "range_min_m": range_min_m,
    "range_max_m": range_max_m,
    "nearest": nearest,
    "points": points,
    "scan_hz": round(scan_hz, 2),
    "model": model,
  }


def sector_min(scan: dict[str, Any], center_deg: float, width_deg: float) -> float | None:
  """Closest return within `width_deg` around `center_deg` (0 ahead, 90 left, -90 right)."""
  ranges = scan["ranges_m"]
  step = scan.get("angle_increment_deg") or 360.0 / len(ranges)
  half = width_deg / 2.0
  best = None
  for index, distance in enumerate(ranges):
    if distance is None:
      continue
    delta = (index * step - center_deg + 180.0) % 360.0 - 180.0
    if abs(delta) <= half and (best is None or distance < best):
      best = distance
  return best


def _open_serial(port: str, baud: int) -> Any:
  try:
    import serial  # type: ignore[import-not-found]
  except ImportError as exc:
    raise HardwareUnavailable("lidar needs pyserial: pip install pyserial (or sudo apt install python3-serial)") from exc
  return serial.Serial(port, baud, timeout=0.1)


def find_lidar_port(ports: Iterable[tuple[str, str | None]]) -> str:
  candidates = [path for path, usb_id in ports if usb_id in _LIDAR_USB_IDS]
  if len(candidates) == 1:
    return candidates[0]
  if not candidates:
    raise HardwareUnavailable("no LiDAR found on USB (its adapter is a CP210x); plug it in or set port: /dev/ttyUSB0")
  raise HardwareUnavailable(f"several CP210x USB-serial devices ({', '.join(candidates)}); set port: to the LiDAR's")


class Lidar(LiveNode):
  """Publishes whole LiDAR revolutions, read and parsed on a background thread."""

  rate_hz = 20.0

  def __init__(
    self,
    *,
    model: str,
    port: str = "auto",
    baud: int | None = None,
    topic: str = "scan",
    bins: int = 360,
    range_min_m: float = 0.05,
    range_max_m: float = 12.0,
    mount_deg: float = 0.0,
    upside_down: bool = False,
    motor_pwm: int = 660,
    timeout_s: float = 2.0,
    mock_range_m: float | None = None,
    backend: str = "mock",
    open_serial: Callable[[str, int], Any] | None = None,
  ) -> None:
    if model not in MODELS:
      raise ValueError(f"model must be one of {', '.join(MODELS)} (RPLidar A1/A2/A3/C1/S1 are all 'rplidar')")
    if not 36 <= bins <= 1440:
      raise ValueError("bins must be 36..1440")
    if not 0 <= range_min_m < range_max_m:
      raise ValueError("need 0 <= range_min_m < range_max_m")
    if not 0 <= motor_pwm <= 1023:
      raise ValueError("motor_pwm must be 0..1023")
    if mock_range_m is not None and mock_range_m <= 0:
      raise ValueError("mock_range_m must be > 0")
    self.model = model
    self.port_name = port
    self.baud = int(baud) if baud is not None else DEFAULT_BAUD[model]
    self.topic = topic
    self.bins = int(bins)
    self.range_min_m = float(range_min_m)
    self.range_max_m = float(range_max_m)
    self.mount_deg = float(mount_deg)
    self.upside_down = bool(upside_down)
    self.motor_pwm = int(motor_pwm)
    self.timeout_ns = int(timeout_s * 1e9)
    self.mock_range_m = mock_range_m
    self.backend_name = backend
    self._open_serial = open_serial or _open_serial
    self._port: Any = None
    self._parser: LdParser | RpParser = RpParser() if model == "rplidar" else LdParser()
    self._lock = threading.Lock()
    self._stop = threading.Event()
    self._thread: threading.Thread | None = None
    self._latest: dict[str, Any] | None = None
    self._latest_seq = 0
    self._published_seq = 0
    self._scan_times: deque[float] = deque(maxlen=20)
    self._last_scan_ns: int | None = None
    self._silent_reported = False
    self._error: str | None = None
    self.scans = 0

  def setup(self, ctx: NodeContext) -> None:
    resolved = resolve_backend(self.backend_name)
    if self.backend_name == "auto":
      ctx.fault(f"hardware backend auto resolved to {resolved}", severity="info", kind="hardware")
    self._last_scan_ns = ctx.now_ns
    if resolved == "mock":
      self.port_name = "mock"
      return
    if self.port_name == "auto":
      from robot_core.link import _detected_ports

      self.port_name = find_lidar_port(_detected_ports())
    self._port = self._open_serial(self.port_name, self.baud)
    if self.model == "rplidar":
      self._start_rplidar()
    self._thread = threading.Thread(target=self._read_loop, name=f"nervlynx-lidar-{self.topic}", daemon=True)
    self._thread.start()

  def _start_rplidar(self) -> None:
    port = self._port
    port.write(rp_command(_RP_STOP))
    time.sleep(0.01)
    if hasattr(port, "reset_input_buffer"):
      port.reset_input_buffer()
    if hasattr(port, "dtr"):
      port.dtr = False  # A1 and C1 adapters: DTR low runs the motor
    port.write(rp_command(_RP_MOTOR_PWM, self.motor_pwm.to_bytes(2, "little")))  # A2/A3; ignored by A1
    port.write(rp_command(_RP_SCAN))

  def _read_loop(self) -> None:
    while not self._stop.is_set():
      try:
        data = self._port.read(4096)
      except Exception as exc:  # noqa: BLE001 - reported from the executor thread
        self._error = f"{type(exc).__name__}: {exc}"
        return
      if not data:
        continue
      for points in self._parser.feed(data):
        self._store(points)

  def _store(self, points: list[tuple[float, float]]) -> None:
    now = time.monotonic()
    self._scan_times.append(now)
    times = self._scan_times
    scan_hz = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 and times[-1] > times[0] else 0.0
    ranges = bin_scan(
      points, bins=self.bins, range_min_m=self.range_min_m, range_max_m=self.range_max_m, mount_deg=self.mount_deg, upside_down=self.upside_down
    )
    payload = scan_payload(ranges, range_min_m=self.range_min_m, range_max_m=self.range_max_m, points=len(points), scan_hz=scan_hz, model=self.model)
    with self._lock:
      self._latest = payload
      self._latest_seq += 1

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    now = ctx.now_ns
    if self._port is None:
      if self.scans and self._last_scan_ns is not None and now - self._last_scan_ns < 100_000_000:
        return None
      ranges = [None if self.mock_range_m is None else round(self.mock_range_m, 3)] * self.bins
      self._last_scan_ns = now
      self.scans += 1
      return [(self.topic, "LaserScan", scan_payload(ranges, range_min_m=self.range_min_m, range_max_m=self.range_max_m, points=0, scan_hz=10.0, model="mock"))]
    if self._error is not None:
      error, self._error = self._error, None
      raise HardwareUnavailable(f"lidar on {self.port_name} stopped: {error}")
    with self._lock:
      payload, seq = self._latest, self._latest_seq
    if payload is None or seq == self._published_seq:
      silent = now - (self._last_scan_ns if self._last_scan_ns is not None else now)
      if silent > self.timeout_ns and not self._silent_reported:
        self._silent_reported = True
        hint = "check model, port, and baud" + (" (A2M12/A3/S1 use 256000, C1 uses 460800)" if self.model == "rplidar" else "")
        ctx.fault(f"lidar: no complete scan from {self.port_name} for {silent / 1e9:.1f} s; {hint}", kind="lidar", severity="error")
      return None
    self._silent_reported = False
    self._published_seq = seq
    self._last_scan_ns = now
    self.scans += 1
    return [(self.topic, "LaserScan", payload)]

  def teardown(self, ctx: NodeContext) -> None:
    self._stop.set()
    if self._thread is not None:
      self._thread.join(timeout=1.0)
    if self._port is not None:
      if self.model == "rplidar":
        try:
          self._port.write(rp_command(_RP_STOP))
          self._port.write(rp_command(_RP_MOTOR_PWM, (0).to_bytes(2, "little")))
          if hasattr(self._port, "dtr"):
            self._port.dtr = True
        except Exception:  # noqa: BLE001 - best effort on the way out
          pass
      self._port.close()
      self._port = None

  def status(self) -> dict[str, Any]:
    with self._lock:
      latest = self._latest
    return {
      "model": self.model,
      "port": self.port_name,
      "scan_topic": self.topic,
      "scans": self.scans,
      "scan_hz": latest["scan_hz"] if latest else None,
      "range_max_m": self.range_max_m,
      "bad_packets": getattr(self._parser, "bad_packets", getattr(self._parser, "bad_samples", 0)),
    }


def ray_scan(world: Any, x: float, y: float, heading_rad: float, *, bins: int, range_max_m: float) -> list[float | None]:
  """What an ideal LiDAR at (x, y) facing `heading_rad` sees in a sim world."""
  step = 2 * math.pi / bins
  out: list[float | None] = []
  for index in range(bins):
    distance = world.ray(x, y, heading_rad + index * step, math.inf)
    out.append(round(distance, 3) if distance <= range_max_m else None)
  return out
