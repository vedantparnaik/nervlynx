import os
import shutil
import socket
import tempfile
import time
from pathlib import Path

import pytest

from robot_core.session import SessionOptions, run_session
from robot_core.systemd import Notifier, SystemdWatchdog, start_systemd_watchdog, unit_timeout_s

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix sockets")


@pytest.fixture
def systemd():
  # Socket paths are limited to about 100 bytes, which pytest's tmp_path can exceed on macOS.
  directory = tempfile.mkdtemp(prefix="nlx-", dir="/tmp")
  path = os.path.join(directory, "notify")
  sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
  sock.bind(path)
  sock.settimeout(2.0)
  yield path, sock
  sock.close()
  shutil.rmtree(directory)


class FakeRuntime:
  def __init__(self) -> None:
    self.started = True
    self.steps = 0
    self.stopping = False
    self.age_s = 0.0

  def last_step_age_s(self) -> float:
    return self.age_s


def pending(sock: socket.socket) -> list[str]:
  out = []
  sock.setblocking(False)
  try:
    while True:
      out.append(sock.recv(4096).decode())
  except BlockingIOError:
    pass
  finally:
    sock.settimeout(2.0)
  return out


def test_arms_once_running_feeds_while_stepping_and_hands_over_at_shutdown(systemd) -> None:
  path, sock = systemd
  rt = FakeRuntime()
  watchdog = SystemdWatchdog(rt, Notifier(path), timeout_s=0.2)
  watchdog.start()
  time.sleep(0.15)
  assert pending(sock) == []
  rt.steps = 1
  assert sock.recv(4096).decode().split("\n") == ["READY=1", "WATCHDOG=1", "WATCHDOG_USEC=200000"]
  assert sock.recv(4096) == b"WATCHDOG=1"
  rt.age_s = 1.0
  time.sleep(0.1)
  pending(sock)
  time.sleep(0.2)
  assert pending(sock) == []
  rt.age_s = 0.0
  assert sock.recv(4096) == b"WATCHDOG=1"
  rt.stopping = True
  watchdog.join(1.0)
  assert pending(sock)[-1].split("\n") == ["WATCHDOG=1", "WATCHDOG_USEC=30000000"]
  watchdog.stop()
  assert pending(sock) == []


def test_a_watchdog_set_in_the_unit_is_kept(systemd) -> None:
  path, sock = systemd
  pid = str(os.getpid())
  assert unit_timeout_s({"WATCHDOG_USEC": "2000000", "WATCHDOG_PID": pid}) == 2.0
  assert unit_timeout_s({"WATCHDOG_USEC": "2000000", "WATCHDOG_PID": "1"}) is None
  assert unit_timeout_s({"WATCHDOG_USEC": "zero"}) is None
  env = {"NOTIFY_SOCKET": path, "WATCHDOG_USEC": "400000", "WATCHDOG_PID": pid}
  rt = FakeRuntime()
  rt.steps = 1
  watchdog = start_systemd_watchdog(rt, 5.0, environ=env)
  assert watchdog is not None and watchdog.timeout_s == 0.4
  assert env == {}
  assert sock.recv(4096).decode().split("\n") == ["READY=1", "WATCHDOG=1"]
  watchdog.stop()


def test_nothing_happens_outside_systemd_and_none_only_reports_ready(systemd) -> None:
  path, sock = systemd
  assert start_systemd_watchdog(FakeRuntime(), 5.0, environ={}) is None
  assert start_systemd_watchdog(FakeRuntime(), 5.0, environ={"NOTIFY_SOCKET": "vsock:2:1234"}) is None
  abstract = Notifier.from_env({"NOTIFY_SOCKET": "@nervlynx"})
  assert abstract is not None and abstract.address == "\0nervlynx"
  abstract.close()
  rt = FakeRuntime()
  rt.steps = 1
  watchdog = start_systemd_watchdog(rt, None, environ={"NOTIFY_SOCKET": path})
  assert sock.recv(4096) == b"READY=1"
  watchdog.join(1.0)
  watchdog.stop()
  assert pending(sock) == []


def test_a_session_under_systemd_feeds_the_watchdog(systemd, tmp_path: Path, monkeypatch) -> None:
  path, sock = systemd
  monkeypatch.setenv("NOTIFY_SOCKET", path)
  cfg = tmp_path / "robot.yaml"
  cfg.write_text(
    "name: dog\n"
    "runtime: {systemd_watchdog_s: 1}\n"
    "nodes:\n"
    "  - {plugin: scripted_drive, rate_hz: 20, params: {steps: [{linear: 0.5, duration_s: 1}]}}\n"
  )
  lines: list[str] = []
  code = run_session(cfg, SessionOptions(duration_s=1.2, no_server=True, quiet=True, run_dir=tmp_path / "run"), lines.append)
  assert code == 0, lines
  assert "systemd_watchdog=1s" in lines
  assert "NOTIFY_SOCKET" not in os.environ
  messages = pending(sock)
  assert messages[0].split("\n") == ["READY=1", "WATCHDOG=1", "WATCHDOG_USEC=1000000"]
  assert "WATCHDOG=1" in messages[1:-1]
  assert messages[-1].split("\n") == ["WATCHDOG=1", "WATCHDOG_USEC=30000000"]
