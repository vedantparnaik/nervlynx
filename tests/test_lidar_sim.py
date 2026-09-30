import json
from urllib.request import urlopen

import pytest

from robot_core.lidar import sector_min
from robot_core.live import LiveRuntime
from robot_core.runtime import SimulatedClock
from robot_core.server import serve_live
from robot_core.sim import SkidSteerSim


def sim_runtime(**params) -> tuple[LiveRuntime, SkidSteerSim, list]:
  sim = SkidSteerSim(world={"width_m": 4.0, "height_m": 3.0, "obstacles": [{"box": [3.0, 1.0, 3.5, 2.0]}]}, start=[1.0, 1.5, 0.0], **params)
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("sim", sim, rate_hz=50)
  scans: list = []
  rt.subscribe("probe", "scan", scans.append)
  return rt, sim, scans


def test_the_simulated_lidar_sees_the_room_counter_clockwise() -> None:
  rt, sim, scans = sim_runtime(lidar={"range_max_m": 5.0})
  rt.run(duration_s=1.0)
  assert len(scans) == 10 and scans[-1].payload["scan_hz"] == 10.0 and scans[-1].payload["model"] == "sim"
  scan = scans[-1].payload
  ranges = scan["ranges_m"]
  assert ranges[0] == pytest.approx(2.0)      # the box face at x = 3.0
  assert ranges[90] == pytest.approx(1.5)     # left wall at y = 3.0
  assert ranges[180] == pytest.approx(1.0)    # back wall at x = 0
  assert ranges[270] == pytest.approx(1.5)    # right wall at y = 0
  assert sector_min(scan, 0, 30) == pytest.approx(2.0)
  assert scan["nearest"]["distance_m"] == 1.0 and scan["nearest"]["angle_deg"] == pytest.approx(180.0, abs=1.0)
  assert sim.status()["lidar"] == {"topic": "scan", "range_max_m": 5.0}


def test_lidar_range_limits_and_validation() -> None:
  rt, _, scans = sim_runtime(lidar={"range_max_m": 1.2, "bins": 90, "every_n_ticks": 10})
  rt.run(duration_s=0.5)
  ranges = scans[-1].payload["ranges_m"]
  assert len(ranges) == 90 and ranges[0] is None and ranges[45] == pytest.approx(1.0)
  with pytest.raises(ValueError, match="lidar needs a world"):
    SkidSteerSim(lidar={})
  with pytest.raises(ValueError, match="unknown fields fov"):
    SkidSteerSim(world={}, lidar={"fov": 1})
  with pytest.raises(ValueError, match="must be numbers"):
    SkidSteerSim(world={}, lidar={"bins": "many"})


def test_topic_endpoint_serves_the_full_scan() -> None:
  rt, _, _ = sim_runtime(lidar={})
  rt.run(duration_s=0.2)
  assert rt.last_payload("nope") is None
  copy = rt.last_payload("scan")
  copy["ranges_m"].clear()
  assert len(rt.last_payload("scan")["ranges_m"]) == 360
  server = serve_live(rt, host="127.0.0.1", port=0)
  try:
    base = f"http://127.0.0.1:{server.server_address[1]}"
    with urlopen(base + "/topic/scan", timeout=3) as resp:
      assert len(json.loads(resp.read())["ranges_m"]) == 360
    with pytest.raises(Exception, match="404"):
      urlopen(base + "/topic/range.front", timeout=3)
    stats = json.loads(urlopen(base + "/stats", timeout=3).read())
    assert str(stats["topics"]["scan"]["last"]).startswith("<")  # too big for the summary
  finally:
    server.shutdown()
    server.server_close()
