from __future__ import annotations

import time


def monotonic_ns() -> int:
  return time.monotonic_ns()


def sleep_to_rate(hz: float, started_ns: int) -> None:
  period_ns = int(1e9 / hz)
  elapsed = monotonic_ns() - started_ns
  remaining = period_ns - elapsed
  if remaining > 0:
    time.sleep(remaining / 1e9)
