"""Let systemd restart NervLynx when the whole process freezes.

Services from `nervlynx deploy --service` (and `deploy/systemd/`) set `NotifyAccess=main`,
so systemd gives the process a notification socket. Once the executor is stepping,
`SystemdWatchdog` asks systemd for a watchdog and feeds it for as long as steps keep
completing. If the process stops (a deadlock, a C extension that never releases the GIL,
SIGSTOP), the pings stop with it: systemd kills the service, faulthandler puts every
thread's stack in the journal, and `Restart=on-failure` starts it again.

The watchdog is armed only once the robot is running, so slow start-up (loading a model,
calibrating an IMU) never trips it. It restarts software; it cannot stop motors, because
a frozen process cannot drive its pins. Hardware that stops by itself covers that: the
`heartbeat` node, or a motor board with its own command timeout such as the ESP32 link.
"""

from __future__ import annotations

import faulthandler
import os
import socket
import threading
from typing import Any, Mapping, MutableMapping

DEFAULT_TIMEOUT_S = 5.0
SHUTDOWN_BUDGET_S = 30.0


def _socket_address(value: str) -> str | None:
  if value.startswith("@"):
    return "\0" + value[1:]
  if value.startswith("/"):
    return value
  return None


class Notifier:
  """Sends sd_notify datagrams. Failures are ignored: systemd is never required."""

  def __init__(self, address: str) -> None:
    self.address = address
    self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    self._sock.settimeout(0.5)

  @classmethod
  def from_env(cls, environ: Mapping[str, str]) -> Notifier | None:
    address = _socket_address(environ.get("NOTIFY_SOCKET", ""))
    if address is None or not hasattr(socket, "AF_UNIX"):
      return None
    try:
      return cls(address)
    except OSError:
      return None

  def send(self, *fields: str) -> bool:
    try:
      self._sock.sendto("\n".join(fields).encode(), self.address)
    except OSError:
      return False
    return True

  def close(self) -> None:
    self._sock.close()


def unit_timeout_s(environ: Mapping[str, str], pid: int | None = None) -> float | None:
  """The timeout from the unit's own `WatchdogSec=`, when systemd set one for this process."""
  owner = environ.get("WATCHDOG_PID")
  if owner and owner != str(os.getpid() if pid is None else pid):
    return None
  try:
    usec = int(environ.get("WATCHDOG_USEC", ""))
  except ValueError:
    return None
  return usec / 1e6 if usec > 0 else None


class SystemdWatchdog:
  """Feeds systemd's watchdog while `runtime` keeps completing executor steps.

  With `request`, the first ping also sets the timeout (`WATCHDOG_USEC`), which arms a
  watchdog the unit file does not mention. `timeout_s=None` only reports `READY=1`.
  """

  def __init__(self, runtime: Any, notifier: Notifier, *, timeout_s: float | None, request: bool = True) -> None:
    self.runtime = runtime
    self.notifier = notifier
    self.timeout_s = timeout_s
    self._request = request
    # Pings stop once the executor is a quarter of the timeout late, so systemd acts
    # between one and one and a quarter timeouts after it stopped.
    self._interval_s = timeout_s / 4 if timeout_s else 0.25
    self._max_step_age_s = self._interval_s
    self._halt = threading.Event()
    self._stopped = False
    self.ready = False
    self._thread = threading.Thread(target=self._run, name="nervlynx-systemd-watchdog", daemon=True)

  def start(self) -> None:
    self._thread.start()

  def stop(self) -> None:
    """Stop feeding the watchdog and give shutdown a fixed budget instead (idempotent)."""
    if self._stopped:
      return
    self._stopped = True
    self._halt.set()
    if self.ready and self.timeout_s:
      # Shutdown does not step the executor. A shutdown that hangs is still killed.
      self.notifier.send("WATCHDOG=1", f"WATCHDOG_USEC={int(SHUTDOWN_BUDGET_S * 1e6)}")

  def join(self, timeout_s: float | None = None) -> None:
    self._thread.join(timeout_s)

  def _run(self) -> None:
    while not self._halt.wait(self._interval_s):
      rt = self.runtime
      if rt.stopping:
        self.stop()
        return
      if not rt.started or rt.steps == 0 or rt.last_step_age_s() > self._max_step_age_s:
        continue
      if self.ready:
        self.notifier.send("WATCHDOG=1")
        continue
      fields = ["READY=1"]
      if self.timeout_s:
        # Ping in the same message so the new timeout counts from now, not from start-up.
        fields.append("WATCHDOG=1")
        if self._request:
          fields.append(f"WATCHDOG_USEC={int(self.timeout_s * 1e6)}")
      self.ready = self.notifier.send(*fields)
      if self.ready and not self.timeout_s:
        return


def start_systemd_watchdog(
  runtime: Any,
  timeout_s: float | None = DEFAULT_TIMEOUT_S,
  environ: MutableMapping[str, str] | None = None,
) -> SystemdWatchdog | None:
  """Feed systemd's watchdog when running under a unit that allows it; None otherwise.

  `timeout_s` is how long the executor may stop before systemd restarts the service
  (None: never). A `WatchdogSec=` in the unit wins over it.
  """
  env = os.environ if environ is None else environ
  notifier = Notifier.from_env(env)
  if notifier is None:
    return None
  configured = unit_timeout_s(env)
  for key in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID"):
    env.pop(key, None)  # so child processes cannot notify on our behalf
  if not faulthandler.is_enabled():
    try:
      faulthandler.enable()  # a watchdog kill is SIGABRT; this dumps every thread's stack first
    except (AttributeError, OSError, ValueError):
      pass
  watchdog = SystemdWatchdog(runtime, notifier, timeout_s=configured or timeout_s, request=configured is None)
  watchdog.start()
  return watchdog
