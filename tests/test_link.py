import json

import pytest

from robot_core.hardware import HardwareUnavailable
from robot_core.link import Esp32Link, SimulatedLinkBoard, find_link_port
from robot_core.live import LiveRuntime
from robot_core.live_config import build_live_runtime, validate_live_config
from robot_core.project import build_registry
from robot_core.runtime import SimulatedClock


def link_runtime(link: Esp32Link) -> tuple[LiveRuntime, list]:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("link", link)
  encoders: list = []
  rt.subscribe("probe", "link.encoders", encoders.append)
  rt.start()
  return rt, encoders


def advance(rt: LiveRuntime, seconds: float) -> None:
  for _ in range(int(round(seconds / 0.02))):
    rt.clock.advance_ms(20)
    rt.step()


def test_link_drives_the_board_and_publishes_encoders() -> None:
  link = Esp32Link()
  rt, encoders = link_runtime(link)
  rt.publish_external("cmd.drive", "DriveCommand", {"linear": 0.5, "angular": 0.0})
  advance(rt, 0.1)
  board = link._port
  assert (board.left, board.right) == (0.5, 0.5)
  assert encoders[-1].payload["left_ticks"] > encoders[0].payload["left_ticks"]
  assert link.status()["firmware"] == {"fw": "nervlynx-link-sim", "v": 1, "board": "sim"}
  assert link.status()["connected"] is True


def test_stale_commands_and_estop_send_zeros() -> None:
  link = Esp32Link(deadman_s=0.1)
  rt, _ = link_runtime(link)
  rt.publish_external("cmd.drive", "DriveCommand", {"left": 0.8, "right": -0.8})
  advance(rt, 0.04)
  assert link._port.left == 0.8
  advance(rt, 0.2)
  assert (link._port.left, link._port.right) == (0.0, 0.0)
  rt.publish_external("cmd.drive", "DriveCommand", {"left": 0.8, "right": 0.8})
  rt.request_estop("test", source="test")
  advance(rt, 0.04)
  assert link._port.left == 0.0


def test_board_watchdog_and_errors_become_faults() -> None:
  link = Esp32Link()
  rt, _ = link_runtime(link)
  link._port.stall(20)
  link._port._send({"t": "err", "msg": "encoder B missing"})
  link._port._tx += b"not json\n"
  advance(rt, 0.04)
  messages = [event.message for event in rt.fault_events]
  assert "link board stopped the motors: its watchdog saw no drive commands" in messages
  assert "link board error: encoder B missing" in messages
  assert link.status()["watchdog_trips"] == 1 and link.status()["bad_lines"] == 1


class SilentPort:
  in_waiting = 0

  def __init__(self):
    self.written = b""
    self.closed = False

  def write(self, data):
    self.written += data

  def read(self, size):
    return b""

  def close(self):
    self.closed = True


def test_a_silent_board_is_reported_and_still_gets_stop_commands() -> None:
  port = SilentPort()
  link = Esp32Link(port="/dev/ttyUSB0", backend="gpiozero", open_serial=lambda name, baud: port)
  rt, _ = link_runtime(link)
  advance(rt, 1.2)
  assert link.status()["connected"] is False
  assert any("no data from the board on /dev/ttyUSB0" in event.message for event in rt.fault_events)
  sent = [json.loads(line) for line in port.written.splitlines()]
  assert sent[0] == {"t": "hello"} and sent[-1] == {"t": "drive", "l": 0.0, "r": 0.0}
  rt.shutdown()
  assert port.closed


def test_firmware_protocol_mismatch_is_flagged() -> None:
  board = SimulatedLinkBoard()
  link = Esp32Link(port="/dev/ttyACM0", backend="gpiozero", open_serial=lambda name, baud: board)
  rt, _ = link_runtime(link)
  board._tx.clear()
  board._send({"t": "hello", "fw": "old", "v": 0, "board": "esp32"})
  advance(rt, 0.02)
  assert any("protocol v0" in event.message for event in rt.fault_events)


def test_port_auto_detection() -> None:
  assert find_link_port([("/dev/ttyUSB0", "10c4:ea60"), ("/dev/ttyACM0", "1546:01a8")]) == "/dev/ttyUSB0"
  with pytest.raises(HardwareUnavailable, match="no ESP32/Pico found"):
    find_link_port([("/dev/ttyACM0", "1546:01a8")])
  with pytest.raises(HardwareUnavailable, match="several boards"):
    find_link_port([("/dev/ttyUSB0", "10c4:ea60"), ("/dev/ttyACM0", "303a:1001")])


def test_link_from_config_with_mock_backend() -> None:
  reg = build_registry()
  cfg = {"nodes": [{"plugin": "esp32_link", "params": {"max_speed": 0.5}}, {"plugin": "scripted_drive", "rate_hz": 20, "params": {"steps": [{"linear": 1.0, "duration_s": 1.0}]}}]}
  assert validate_live_config(cfg, reg) == []
  rt = build_live_runtime(cfg, reg, clock=SimulatedClock())
  rt.run(duration_s=0.5)
  assert rt.nodes["esp32_link"].status()["encoders"]["left_ticks"] > 0
