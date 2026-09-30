import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import yaml

from robot_core.drive import SkidSteerDrive
from robot_core.live import LiveRuntime
from robot_core.runtime import SimulatedClock
from robot_core.sensors import MPU6050Imu
from robot_core.server import serve_live


def request(base: str, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
  headers = {"X-NervLynx-Token": token} if token else {}
  data = None if body is None else json.dumps(body).encode("utf-8")
  req = Request(base + path, data=data, headers=headers, method="POST" if body is not None else "GET")
  try:
    with urlopen(req, timeout=5) as resp:
      return resp.status, json.loads(resp.read())
  except HTTPError as exc:
    return exc.code, json.loads(exc.read())


@pytest.fixture()
def robot(tmp_path: Path):
  config = tmp_path / "robot.yaml"
  config.write_text("nodes: []\n", encoding="utf-8")
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  drive = SkidSteerDrive(
    driver="l298n", left=[{"name": "left", "in1": 5, "in2": 6}], right=[{"name": "right", "in1": 20, "in2": 21}], max_speed=0.6
  )
  rt.add_node("drive", drive, rate_hz=50)
  rt.add_node("imu", MPU6050Imu())
  rt.start()
  stop = threading.Event()

  def spin() -> None:
    while not stop.is_set():
      rt.clock.advance_ms(20)
      rt.step()
      stop.wait(0.002)

  stepper = threading.Thread(target=spin, daemon=True)
  stepper.start()
  servers = []

  def start(**kwargs):
    server = serve_live(rt, host="127.0.0.1", port=0, config_path=config, **kwargs)
    servers.append(server)
    return f"http://127.0.0.1:{server.server_address[1]}"

  yield rt, drive, start, tmp_path
  stop.set()
  stepper.join()
  for server in servers:
    server.shutdown()
    server.server_close()


def test_overview_lists_calibratable_nodes_without_control(robot) -> None:
  _, _, start, _ = robot
  base = start()
  code, body = request(base, "/calibration")
  assert code == 200 and set(body["nodes"]) == {"drive", "imu"}
  assert body["nodes"]["drive"]["kind"] == "drive" and body["nodes"]["imu"]["axes"] == ["+x", "+y", "+z"]
  assert body["can_save"] is False and body["saved"] is None
  assert request(base, "/calibration/drive", {"action": "spin", "motor": "left"})[0] == 403


def test_wizard_steps_run_on_the_robot_and_save_calibration(robot) -> None:
  rt, drive, start, folder = robot
  base = start(allow_control=True, control_token="t0k")
  assert request(base, "/calibration", token="t0k")[1]["can_save"] is True
  code, body = request(base, "/calibration/drive", {"action": "invert", "motor": "right", "value": True}, token="t0k")
  assert code == 200 and body["sides"]["right"] == [{"name": "right", "invert": True}]
  code, body = request(base, "/calibration/drive", {"action": "spin", "motor": "nope"}, token="t0k")
  assert code == 400 and "no motor named 'nope'" in body["error"]
  assert request(base, "/calibration/lidar", {"action": "describe"}, token="t0k")[0] == 404
  assert request(base, "/calibration/drive", {"motor": "left"}, token="t0k")[0] == 400
  code, body = request(base, "/calibration/save", {}, token="t0k")
  assert code == 200 and body["saved"] == {"drive": {"params": {"right": [{"name": "right", "invert": True}]}}}
  saved = yaml.safe_load((folder / "calibration.yaml").read_text(encoding="utf-8"))
  assert saved["nodes"]["drive"]["params"]["right"] == [{"name": "right", "invert": True}]
  assert request(base, "/calibration", token="t0k")[1]["saved"] == saved


def test_motor_tests_are_refused_under_estop(robot) -> None:
  rt, _, start, _ = robot
  base = start(allow_control=True)
  request(base, "/estop", {"reason": "test"})
  code, body = request(base, "/calibration/drive", {"action": "test", "move": "forward"})
  assert code == 400 and "clear the e-stop first" in body["error"]


def test_saving_with_nothing_calibrated_says_so(robot) -> None:
  _, _, start, folder = robot
  base = start(allow_control=True)
  code, body = request(base, "/calibration/save", {})
  assert code == 200 and body["message"] == "nothing has been calibrated yet"
  assert not (folder / "calibration.yaml").exists()


def test_dashboard_ships_the_wizard() -> None:
  from robot_core.server import _DASHBOARD_HTML

  assert 'id="calib"' in _DASHBOARD_HTML and "loadCalibration()" in _DASHBOARD_HTML
