from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SafetySnapshot:
  mode: str
  reason: str
  emergency_stop: bool


class SafetySupervisor:
  def __init__(self, caution_ttl_s: float = 2.0, mrs_hold_s: float = 5.0) -> None:
    self._mode = "normal"
    self._reason = "ok"
    self._caution_ttl_ns = int(caution_ttl_s * 1e9)
    self._mrs_hold_ns = int(mrs_hold_s * 1e9)
    self._last_warning_ns = 0
    self._last_critical_ns = 0

  def observe_fault(self, severity: str, message: str, now_ns: int) -> None:
    s = severity.lower()
    if s == "critical":
      self._last_critical_ns = now_ns
      self._mode = "mrs"
      self._reason = message
      return
    self._last_warning_ns = now_ns
    if self._mode != "mrs":
      self._mode = "caution"
      self._reason = message

  def tick(self, now_ns: int) -> SafetySnapshot:
    if self._mode == "mrs":
      if now_ns - self._last_critical_ns > self._mrs_hold_ns:
        self._mode = "caution"
        self._reason = "critical fault hold elapsed"
        self._last_warning_ns = now_ns
    if self._mode == "caution":
      if now_ns - self._last_warning_ns > self._caution_ttl_ns and now_ns - self._last_critical_ns > self._mrs_hold_ns:
        self._mode = "normal"
        self._reason = "ok"
    return SafetySnapshot(mode=self._mode, reason=self._reason, emergency_stop=self._mode == "mrs")
