from pathlib import Path

import pytest

import robot_core.hardware as hardware
from robot_core.drive import SkidSteerDrive
from robot_core.hardware import BoardInfo, HardwareUnavailable, MockBackend, create_backend, detect_board, resolve_backend
from robot_core.live import LiveRuntime
from robot_core.runtime import SimulatedClock

LEFT = [{"name": "FL", "rpwm": 12, "lpwm": 13}]
RIGHT = [{"name": "FR", "rpwm": 18, "lpwm": 19, "invert": True}]
PI5 = BoardInfo("Raspberry Pi 5 Model B Rev 1.0")
PI4 = BoardInfo("Raspberry Pi 4 Model B Rev 1.4")
LAPTOP = BoardInfo(None)


def only(*modules: str):
  return lambda module: module in modules


def test_detect_board_reads_the_nul_terminated_device_tree_model(tmp_path: Path) -> None:
  model = tmp_path / "model"
  model.write_bytes(b"Raspberry Pi Zero 2 W Rev 1.0\x00")
  assert detect_board(model) == BoardInfo("Raspberry Pi Zero 2 W Rev 1.0")
  assert detect_board(tmp_path / "missing") == BoardInfo(None)


@pytest.mark.parametrize(
  ("model", "is_pi", "has_rp1"),
  [
    ("Raspberry Pi 5 Model B Rev 1.0", True, True),
    ("Raspberry Pi 500 Rev 1.0", True, True),
    ("Raspberry Pi Compute Module 5 Rev 1.0", True, True),
    ("Raspberry Pi 4 Model B Rev 1.4", True, False),
    ("Raspberry Pi Compute Module 4 Rev 1.0", True, False),
    ("Raspberry Pi Zero 2 W Rev 1.0", True, False),
    ("NVIDIA Jetson Orin NX Engineering Reference Developer Kit", False, False),
    (None, False, False),
  ],
)
def test_board_classification(model: str | None, is_pi: bool, has_rp1: bool) -> None:
  board = BoardInfo(model)
  assert (board.is_raspberry_pi, board.has_rp1) == (is_pi, has_rp1)


def test_auto_prefers_gpiozero_on_any_pi() -> None:
  assert resolve_backend("auto", board=PI5, importable=only("gpiozero", "RPi.GPIO")) == "gpiozero"
  assert resolve_backend("auto", board=PI4, importable=only("gpiozero", "RPi.GPIO")) == "gpiozero"


def test_auto_falls_back_to_rpi_gpio_only_where_it_works() -> None:
  assert resolve_backend("auto", board=PI4, importable=only("RPi.GPIO")) == "rpi_gpio"
  with pytest.raises(HardwareUnavailable, match="lgpio"):
    resolve_backend("auto", board=PI5, importable=only("RPi.GPIO"))


def test_auto_fails_loudly_on_a_pi_without_gpio_libraries() -> None:
  with pytest.raises(HardwareUnavailable, match="no GPIO library"):
    resolve_backend("auto", board=PI4, importable=only())


def test_auto_simulates_off_the_pi_and_explicit_names_pass_through() -> None:
  assert resolve_backend("auto", board=LAPTOP, importable=only("gpiozero")) == "mock"
  assert resolve_backend("rpi_gpio", board=PI5, importable=only()) == "rpi_gpio"
  assert isinstance(create_backend("auto", board=LAPTOP), MockBackend)


def test_drive_with_auto_backend_reports_what_it_resolved_to(monkeypatch) -> None:
  monkeypatch.setattr(hardware, "detect_board", lambda: LAPTOP)
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  drive = SkidSteerDrive(left=LEFT, right=RIGHT, backend="auto")
  rt.add_node("drive", drive, rate_hz=50)
  rt.start()
  assert isinstance(drive.backend, MockBackend)
  assert drive.status()["backend"] == "mock"
  assert any(e.message == "hardware backend auto resolved to mock" and e.severity == "info" for e in rt.fault_events)
  rt.shutdown()
