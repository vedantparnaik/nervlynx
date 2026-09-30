import pytest

from robot_core.gps import GpsNmea, GpsState, bearing_deg, distance_m, find_gps_port, nmea_checksum, offset, parse_sentence
from robot_core.hardware import HardwareUnavailable
from robot_core.live import LiveRuntime
from robot_core.live_config import build_live_runtime, validate_live_config
from robot_core.project import build_registry
from robot_core.runtime import SimulatedClock

GGA = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
RMC = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"


def sentence(body: str) -> str:
  return f"${body}*{nmea_checksum(body)}"


def test_sentences_need_a_correct_checksum() -> None:
  assert parse_sentence(GGA)[0] == "GGA" and parse_sentence(RMC)[0] == "RMC"
  assert parse_sentence(GGA.replace("*47", "*48")) is None
  assert parse_sentence("GPGGA,123519*47") is None
  assert parse_sentence(sentence("GNGGA,1,2")) == ("GGA", ["1", "2"])


def test_gga_and_rmc_fill_in_the_fix() -> None:
  state = GpsState()
  assert state.update(*parse_sentence(GGA)) and state.update(*parse_sentence(RMC))
  p = state.payload()
  assert p["fix"] is True and p["quality"] == "gps" and p["sats"] == 8 and p["hdop"] == 0.9
  assert p["lat_deg"] == pytest.approx(48.1173) and p["lon_deg"] == pytest.approx(11.516667, abs=1e-6)
  assert p["alt_m"] == 545.4 and p["utc"] == "12:35:19"
  assert p["speed_mps"] == pytest.approx(22.4 * 0.514444, abs=1e-3) and p["course_deg"] == 84.4
  state.update(*parse_sentence(sentence("GPGGA,123520,3345.1,S,15112.5,W,2,11,1.2,10.0,M,0,M,,")))
  assert state.lat_deg == pytest.approx(-33.751667, abs=1e-6) and state.lon_deg == pytest.approx(-151.208333, abs=1e-6)
  assert state.quality == "dgps"


def test_no_fix_clears_the_position() -> None:
  state = GpsState()
  state.update(*parse_sentence(GGA))
  state.update(*parse_sentence(sentence("GPGGA,123521,,,,,0,03,,,M,,M,,")))
  state.update(*parse_sentence(sentence("GPRMC,123521,V,,,,,,,230394,,,N")))
  assert state.payload() == {
    "fix": False, "lat_deg": None, "lon_deg": None, "alt_m": None, "sats": 3, "hdop": None,
    "speed_mps": None, "course_deg": None, "quality": None, "utc": "12:35:21",
  }


def test_geometry_helpers() -> None:
  origin = (51.4779, -0.0015)
  north = offset(origin, 0.0, 100.0)
  east = offset(origin, 100.0, 0.0)
  assert distance_m(origin, north) == pytest.approx(100.0, rel=1e-3)
  assert distance_m(origin, east) == pytest.approx(100.0, rel=1e-3)
  assert bearing_deg(origin, north) == pytest.approx(0.0, abs=0.01)
  assert bearing_deg(origin, east) == pytest.approx(90.0, abs=0.01)


class ChunkedPort:
  """A serial port that hands out a byte stream in awkward chunks."""

  def __init__(self, data: bytes, chunk: int = 7):
    self.data = data
    self.chunk = chunk
    self.closed = False

  @property
  def in_waiting(self) -> int:
    return min(self.chunk, len(self.data))

  def read(self, size: int) -> bytes:
    out, self.data = self.data[:size], self.data[size:]
    return out

  def close(self):
    self.closed = True


def gps_runtime(node: GpsNmea) -> tuple[LiveRuntime, list]:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("gps", node)
  fixes: list = []
  rt.subscribe("probe", "gps", fixes.append)
  return rt, fixes


def test_the_node_parses_a_split_stream_and_skips_garbage() -> None:
  stream = f"{GGA}\r\n$GPGSV,noise*00\r\n{RMC}\r\n".encode("ascii") + b"\xff" * 300 + f"\n{GGA}\r\n".encode("ascii")
  port = ChunkedPort(stream, chunk=11)
  node = GpsNmea(port="/dev/ttyAMA0", backend="gpiozero", open_serial=lambda name, baud: port)
  rt, fixes = gps_runtime(node)
  rt.run(duration_s=10.0)
  assert fixes and fixes[-1].payload["lat_deg"] == pytest.approx(48.1173)
  assert node.sentences == 3 and node.bad_sentences >= 2 and port.closed


def test_a_silent_receiver_is_reported_with_wiring_hints() -> None:
  node = GpsNmea(port="/dev/serial0", backend="gpiozero", timeout_s=1.0, open_serial=lambda name, baud: ChunkedPort(b""))
  rt, fixes = gps_runtime(node)
  rt.run(duration_s=2.0)
  assert fixes == []
  assert any("no NMEA data on /dev/serial0" in e.message and "GPIO15" in e.message for e in rt.fault_events)


def test_port_detection() -> None:
  assert find_gps_port([("/dev/ttyACM0", "1546:01a8"), ("/dev/ttyUSB0", "10c4:ea60")], uart_exists=True) == "/dev/ttyACM0"
  assert find_gps_port([("/dev/ttyUSB0", "10c4:ea60")], uart_exists=True) == "/dev/serial0"
  with pytest.raises(HardwareUnavailable, match="raspi-config"):
    find_gps_port([], uart_exists=False)


def test_the_simulated_gps_follows_the_sim_robot() -> None:
  reg = build_registry()
  cfg = {
    "nodes": [
      {"plugin": "scripted_drive", "rate_hz": 20, "params": {"steps": [{"linear": 1.0, "duration_s": 5.0}], "loop": False}},
      {"plugin": "skid_steer_drive", "rate_hz": 50, "params": {"left": [{"rpwm": 1, "lpwm": 2}], "right": [{"rpwm": 3, "lpwm": 4}]}},
      {"plugin": "skid_steer_sim", "rate_hz": 50, "params": {"world": {"width_m": 20, "height_m": 20}, "start": [2.0, 10.0, 90.0]}},
      {"plugin": "gps_nmea", "params": {"mock_origin": [10.0, 20.0]}},
    ]
  }
  assert validate_live_config(cfg, reg) == []
  rt = build_live_runtime(cfg, reg, clock=SimulatedClock())
  fixes: list = []
  rt.subscribe("probe", "gps", fixes.append)
  rt.run(duration_s=4.0)
  last = fixes[-1].payload
  odom = rt.nodes["skid_steer_sim"].odometry()
  assert last["quality"] == "simulated" and last["course_deg"] == pytest.approx(0.0, abs=1.0)  # heading 90 = north
  assert distance_m((10.0, 20.0), (last["lat_deg"], last["lon_deg"])) == pytest.approx((odom["x_m"] ** 2 + odom["y_m"] ** 2) ** 0.5, rel=0.02)
