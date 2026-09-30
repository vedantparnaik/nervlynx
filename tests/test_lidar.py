import time

import pytest

from robot_core.lidar import (
  LdParser,
  Lidar,
  RpParser,
  bin_scan,
  find_lidar_port,
  ld_crc8,
  ld_packet,
  rp_command,
  rp_sample,
  sector_min,
)
from robot_core.hardware import HardwareUnavailable
from robot_core.live import LiveRuntime
from robot_core.live_config import validate_live_config
from robot_core.project import build_registry
from robot_core.runtime import SimulatedClock


def revolution(first: int = 0, last: int = 29, distance_mm: int = 1500) -> bytes:
  """LD packets of 12 points 1 degree apart, 12 degrees per packet."""
  return b"".join(ld_packet(k * 12, k * 12 + 11, [distance_mm] * 12, stamp_ms=k) for k in range(first, last + 1))


def test_ld_crc_matches_the_datasheet_table() -> None:
  from robot_core.lidar import _LD_CRC

  assert _LD_CRC[:8] == [0x00, 0x4D, 0x9A, 0xD7, 0x79, 0x34, 0xE3, 0xAE]
  packet = ld_packet(0, 11, [1000] * 12)
  assert len(packet) == 47 and ld_crc8(packet[:-1]) == packet[-1]


def test_ld_parser_yields_whole_revolutions_and_resyncs() -> None:
  parser = LdParser()
  stream = revolution(15, 29) + b"\x00\x54garbage" + revolution() + revolution(distance_mm=800) + ld_packet(0, 11, [0] * 12)
  corrupt = bytearray(revolution(0, 0))
  corrupt[10] ^= 0xFF
  scans = []
  for i in range(0, len(stream), 50):
    scans += parser.feed(stream[i : i + 50])
  scans += parser.feed(bytes(corrupt))
  assert [len(s) for s in scans] == [360, 360]
  assert scans[0][0] == (0.0, 1.5) and scans[1][-1] == (359.0, 0.8)
  assert parser.bad_packets == 1


def test_rplidar_parser_splits_on_the_start_flag() -> None:
  parser = RpParser()
  samples = b"".join(rp_sample(a, 1000 + a, start=(a == 0)) for a in range(0, 360))
  stream = b"noise" + bytes((0xA5, 0x5A, 0x05, 0x00, 0x00, 0x40, 0x81)) + rp_sample(200, 500) + samples
  stream += bytes((0x03, 0x00, 0x00, 0x00, 0x00)) + rp_sample(0, 700, start=True)
  scans = parser.feed(stream[:20]) + parser.feed(stream[20:])
  assert len(scans) == 1 and len(scans[0]) == 360
  assert scans[0][90] == (90.0, pytest.approx(1.09))
  assert parser.bad_samples >= 1
  assert rp_command(0xF0, (660).to_bytes(2, "little")) == bytes((0xA5, 0xF0, 0x02, 0x94, 0x02, 0xA5 ^ 0xF0 ^ 0x02 ^ 0x94 ^ 0x02))


def test_bins_are_counter_clockwise_from_the_front_and_keep_the_closest_point() -> None:
  points = [(90.0, 1.0), (90.2, 0.7), (0.0, 2.0), (10.0, 0.01), (45.0, 20.0)]
  ranges = bin_scan(points, bins=360, range_min_m=0.05, range_max_m=12.0)
  assert ranges[270] == 0.7 and ranges[0] == 2.0  # lidar's right is the robot's -90 degrees
  assert ranges[10] is None and ranges[315] is None  # too close, too far
  assert bin_scan([(90.0, 1.0)], bins=360, range_min_m=0.05, range_max_m=12.0, upside_down=True)[90] == 1.0
  assert bin_scan([(0.0, 1.0)], bins=360, range_min_m=0.05, range_max_m=12.0, mount_deg=180)[180] == 1.0
  scan = {"ranges_m": ranges, "angle_increment_deg": 1.0}
  assert sector_min(scan, 0, 60) == 2.0 and sector_min(scan, -90, 20) == 0.7 and sector_min(scan, 90, 20) is None


class StreamPort:
  def __init__(self, data: bytes):
    self.data = data
    self.written = b""
    self.dtr = True
    self.closed = False

  def read(self, size: int) -> bytes:
    if not self.data:
      time.sleep(0.01)
      return b""
    out, self.data = self.data[:size], self.data[size:]
    return out

  def write(self, data: bytes) -> int:
    self.written += data
    return len(data)

  def reset_input_buffer(self) -> None:
    pass

  def close(self) -> None:
    self.closed = True


def wait_for(predicate, timeout_s: float = 3.0) -> None:
  deadline = time.monotonic() + timeout_s
  while not predicate():
    assert time.monotonic() < deadline, "timed out"
    time.sleep(0.01)


def lidar_runtime(node: Lidar) -> tuple[LiveRuntime, list]:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("lidar", node)
  scans: list = []
  rt.subscribe("probe", "scan", scans.append)
  rt.start()
  return rt, scans


def step(rt: LiveRuntime, ms: int = 50) -> None:
  rt.clock.advance_ms(ms)
  rt.step()


def test_ld19_node_publishes_scans_parsed_on_its_thread() -> None:
  port = StreamPort(revolution(20, 29) + revolution() + revolution(distance_mm=600) + ld_packet(0, 11, [600] * 12))
  node = Lidar(model="ld19", port="/dev/ttyUSB0", backend="gpiozero", open_serial=lambda name, baud: port)
  rt, scans = lidar_runtime(node)
  assert node.baud == 230400
  wait_for(lambda: node._latest_seq >= 2)
  step(rt)
  step(rt)
  assert len(scans) == 1 and scans[0].payload["ranges_m"][0] == 0.6 and scans[0].payload["nearest"] == {"distance_m": 0.6, "angle_deg": 0.0}
  assert node.status()["scan_topic"] == "scan"
  rt.shutdown()
  assert port.closed and not node._thread.is_alive()


def test_rplidar_node_starts_and_stops_the_motor() -> None:
  port = StreamPort(b"")
  node = Lidar(model="rplidar", port="/dev/ttyUSB0", backend="gpiozero", open_serial=lambda name, baud: port)
  rt, _ = lidar_runtime(node)
  assert port.written.startswith(rp_command(0x25)) and port.written.endswith(rp_command(0x20)) and port.dtr is False
  rt.shutdown()
  assert port.written.endswith(rp_command(0xF0, b"\x00\x00")) and port.dtr is True


def test_a_silent_lidar_is_reported() -> None:
  node = Lidar(model="rplidar", port="/dev/ttyUSB0", backend="gpiozero", timeout_s=0.5, open_serial=lambda name, baud: StreamPort(b""))
  rt, scans = lidar_runtime(node)
  for _ in range(15):
    step(rt)
  assert scans == [] and any("256000" in e.message for e in rt.fault_events)
  rt.shutdown()


def test_mock_lidar_reports_a_clear_room_at_10_hz() -> None:
  node = Lidar(model="ld19")
  rt, scans = lidar_runtime(node)
  for _ in range(20):
    step(rt)
  assert 9 <= len(scans) <= 11 and scans[-1].payload["ranges_m"] == [None] * 360
  node = Lidar(model="ld06", bins=180, mock_range_m=1.25)
  rt, scans = lidar_runtime(node)
  step(rt)
  assert scans[-1].payload["ranges_m"] == [1.25] * 180 and scans[-1].payload["angle_increment_deg"] == 2.0


def test_config_and_port_detection() -> None:
  reg = build_registry()
  assert validate_live_config({"nodes": [{"plugin": "lidar", "params": {"model": "ld19"}}]}, reg) == []
  problems = validate_live_config({"nodes": [{"plugin": "lidar", "params": {"model": "xv11"}}]}, reg)
  assert problems == ["nodes[0] (lidar): model must be one of ld19, ld06, rplidar (RPLidar A1/A2/A3/C1/S1 are all 'rplidar')"]
  assert find_lidar_port([("/dev/ttyUSB0", "10c4:ea60"), ("/dev/ttyACM0", "1546:01a8")]) == "/dev/ttyUSB0"
  with pytest.raises(HardwareUnavailable, match="several CP210x"):
    find_lidar_port([("/dev/ttyUSB0", "10c4:ea60"), ("/dev/ttyUSB1", "10c4:ea60")])
