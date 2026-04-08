from __future__ import annotations

from dataclasses import dataclass
import time


@dataclass(frozen=True)
class WatchdogFault:
  node_name: str
  age_s: float
  message: str


class HealthWatchdog:
  """Checks node liveness from runtime heartbeat timestamps."""

  def __init__(self, stale_after_s: float = 1.5) -> None:
    self._stale_after_ns = int(max(0.01, stale_after_s) * 1e9)

  def check(self, heartbeats_ns: dict[str, int], now_ns: int | None = None) -> list[WatchdogFault]:
    now = now_ns if now_ns is not None else time.monotonic_ns()
    faults: list[WatchdogFault] = []
    for node_name, last_ns in heartbeats_ns.items():
      age_ns = now - last_ns
      if age_ns > self._stale_after_ns:
        age_s = age_ns / 1e9
        faults.append(
          WatchdogFault(
            node_name=node_name,
            age_s=age_s,
            message=f"node {node_name} stale for {age_s:.3f}s",
          )
        )
    return faults
