"""Artifacts for live runs: streaming trace and fault logs, plus the end-of-run report."""

from __future__ import annotations

import json
import platform
import threading
import time
from collections import deque
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Iterable

from robot_core.live import FaultEvent, LiveRuntime
from robot_core.recorder import encode_message
from robot_core.runtime import RuntimeMessage


def package_version() -> str:
  try:
    return version("nervlynx")
  except PackageNotFoundError:
    return "nervlynx-unknown"


class LineWriter:
  """Appends lines to a file from its own thread, so a slow SD card never blocks the caller.

  Callers only queue lines. Once `max_buffer_bytes` are waiting, a new line is dropped and
  counted, or with `block_when_full` the caller waits for room: simulated time has no
  deadline to miss, and its traces must be complete to replay. After a write error (a full
  disk, say) the file is abandoned and everything later is dropped; the caller keeps going.
  """

  def __init__(self, path: str | Path, *, name: str, max_buffer_bytes: int, flush_every_s: float, block_when_full: bool = False) -> None:
    self.path = Path(path)
    self.path.parent.mkdir(parents=True, exist_ok=True)
    self._file = self.path.open("w", encoding="utf-8")
    self._max_bytes = max(1, int(max_buffer_bytes))
    self._flush_every_s = max(0.0, float(flush_every_s))
    self._block = block_when_full
    self._cond = threading.Condition()
    self._lines: deque[str] = deque()
    self._buffered = 0
    self._accepted = 0
    self._settled = 0  # accepted lines that are flushed to the OS or given up on
    self._closing = False
    self._flush_wanted = False
    self.written = 0
    self.dropped = 0
    self.error: str | None = None
    self._thread = threading.Thread(target=self._run, name=f"nervlynx-{name}-writer", daemon=True)
    self._thread.start()

  def put(self, line: str) -> bool:
    """Queue one line (with its newline); False if it was dropped."""
    size = len(line)
    with self._cond:
      while self._block and self._full(size) and not self._closing and self.error is None and self._thread.is_alive():
        self._cond.wait(0.5)
      if self._closing or self.error is not None or self._full(size):
        self.dropped += 1
        return False
      self._lines.append(line)
      self._buffered += size
      self._accepted += 1
      if len(self._lines) == 1:
        self._cond.notify_all()
      return True

  def flush(self, timeout_s: float = 5.0) -> bool:
    """Wait until every line queued so far has reached the OS (or been given up on)."""
    with self._cond:
      target = self._accepted
      self._flush_wanted = True
      self._cond.notify_all()
      self._cond.wait_for(lambda: self._settled >= target or not self._thread.is_alive(), timeout_s)
      return self._settled >= target

  def close(self, timeout_s: float = 10.0) -> None:
    """Write what is queued, then close the file. Safe to call more than once."""
    with self._cond:
      self._closing = True
      self._cond.notify_all()
    self._thread.join(timeout_s)
    if self._thread.is_alive():
      self.error = self.error or f"still writing after {timeout_s:g}s; the end of {self.path.name} may be missing"
      return
    try:
      self._file.close()
    except OSError as exc:
      self.error = self.error or f"{type(exc).__name__}: {exc}"

  def _full(self, size: int) -> bool:
    return bool(self._lines) and self._buffered + size > self._max_bytes

  def _run(self) -> None:
    unflushed = 0
    last_flush = time.monotonic()
    while True:
      with self._cond:
        if not (self._lines or self._closing or self._flush_wanted):
          self._cond.wait(self._flush_every_s if unflushed else None)
        batch, self._lines, self._buffered = self._lines, deque(), 0
        closing, flush_now, self._flush_wanted = self._closing, self._flush_wanted, False
        self._cond.notify_all()
      if batch:
        unflushed += self._write("".join(batch), len(batch))
      now = time.monotonic()
      if closing or flush_now or now - last_flush >= self._flush_every_s:
        self._flush_file()
        last_flush = now
        with self._cond:
          self._settled += unflushed
          self._cond.notify_all()
        unflushed = 0
      if closing:
        with self._cond:
          if not self._lines:
            return

  def _write(self, text: str, count: int) -> int:
    if self.error is None:
      try:
        self._file.write(text)
        self.written += count
        return count
      except Exception as exc:
        self.error = f"{type(exc).__name__}: {exc}"
    with self._cond:
      self.dropped += count
      self._settled += count
      self._cond.notify_all()
    return 0

  def _flush_file(self) -> None:
    if self.error is None:
      try:
        self._file.flush()
      except Exception as exc:
        self.error = f"{type(exc).__name__}: {exc}"


class TraceRecorder:
  """Appends every published message to a JSONL trace readable by `robot-core replay`.

  Messages are encoded on the calling thread, since a payload may change after it is
  published, and written by a `LineWriter`.
  """

  def __init__(
    self,
    path: str | Path,
    *,
    exclude_topics: Iterable[str] = (),
    max_messages: int | None = 2_000_000,
    max_buffer_bytes: int = 8 << 20,
    block_when_full: bool = False,
  ) -> None:
    self.path = Path(path)
    self._exclude = frozenset(exclude_topics)
    self._max = max_messages
    self._queued = 0
    self.skipped = 0
    self._writer = LineWriter(self.path, name="trace", max_buffer_bytes=max_buffer_bytes, flush_every_s=1.0, block_when_full=block_when_full)

  @property
  def written(self) -> int:
    return self._writer.written

  @property
  def dropped(self) -> int:
    return self._writer.dropped

  @property
  def error(self) -> str | None:
    return self._writer.error

  def __call__(self, msg: RuntimeMessage) -> None:
    if msg.envelope.topic in self._exclude:
      return
    if self._max is not None and self._queued >= self._max:
      self.skipped += 1
      return
    if self._writer.put(json.dumps(encode_message(msg), separators=(",", ":"), default=str) + "\n"):
      self._queued += 1

  def flush(self, timeout_s: float = 5.0) -> bool:
    return self._writer.flush(timeout_s)

  def close(self) -> None:
    self._writer.close()


class FaultLog:
  """Streams structured faults to JSONL as they happen, from any thread, without waiting on the disk."""

  def __init__(self, path: str | Path, *, block_when_full: bool = False) -> None:
    self.path = Path(path)
    self._writer = LineWriter(self.path, name="faults", max_buffer_bytes=1 << 20, flush_every_s=0.0, block_when_full=block_when_full)

  @property
  def count(self) -> int:
    return self._writer.written

  @property
  def dropped(self) -> int:
    return self._writer.dropped

  @property
  def error(self) -> str | None:
    return self._writer.error

  def __call__(self, event: FaultEvent) -> None:
    self._writer.put(json.dumps(event.to_dict()) + "\n")

  def flush(self, timeout_s: float = 5.0) -> bool:
    return self._writer.flush(timeout_s)

  def close(self) -> None:
    self._writer.close()


def _counter_totals(runtime: LiveRuntime, name: str, label: str) -> dict[str, float]:
  totals: dict[str, float] = {}
  for series in runtime.metrics.snapshot()["counters"].get(name, []):
    key = str(series["labels"].get(label, ""))
    totals[key] = totals.get(key, 0.0) + float(series["value"])
  return dict(sorted(totals.items()))


def build_report(
  runtime: LiveRuntime,
  *,
  config_path: str | Path | None = None,
  wall_started: float,
  wall_finished: float,
  artifacts: dict[str, str] | None = None,
  extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
  snap = runtime.snapshot(fault_limit=100)
  return {
    "report_version": 1,
    "nervlynx_version": package_version(),
    "graph": runtime.name,
    "config_path": str(config_path) if config_path is not None else None,
    "clock": snap["clock"],
    "started_at_unix": wall_started,
    "finished_at_unix": wall_finished,
    "wall_duration_s": round(wall_finished - wall_started, 3),
    "runtime_duration_s": round(snap["health"]["uptime_s"], 3),
    "platform": {
      "python": platform.python_version(),
      "system": platform.system(),
      "machine": platform.machine(),
      "node": platform.node(),
    },
    "health": snap["health"],
    "estop": snap["estop"],
    "executor": snap["executor"],
    "messages": snap["messages"],
    "nodes": snap["nodes"],
    "topics": {topic: {k: v for k, v in stats.items() if k != "last"} for topic, stats in snap["topics"].items()},
    "faults": {
      "by_kind": _counter_totals(runtime, "nervlynx_faults_total", "kind"),
      "by_severity": _counter_totals(runtime, "nervlynx_faults_total", "severity"),
      "recent": snap["faults"],
    },
    "artifacts": artifacts or {},
    **(extra or {}),
  }


def _ms(value: Any) -> str:
  return "-" if value is None else f"{value:.3f}"


def render_markdown(report: dict[str, Any]) -> str:
  msgs, ex, es = report["messages"], report["executor"], report["estop"]
  lines = [
    f"# NervLynx run report: {report['graph']}",
    "",
    f"- Version: {report['nervlynx_version']} | clock: {report['clock']} | "
    f"{report['platform']['system']} {report['platform']['machine']} | Python {report['platform']['python']}",
    f"- Duration: {report['runtime_duration_s']} s runtime ({report['wall_duration_s']} s wall)",
    f"- Final health: **{report['health']['status']}**",
    f"- Messages: {msgs['published']} published, {msgs['delivered']} delivered, {msgs['dropped']} dropped",
    f"- Executor: {ex['steps']} steps, max queue depth {ex['queue_depth_max']}, "
    f"{ex['hop_limit_events']} hop-limit events, {ex['stalls']} stalls",
    f"- E-stop: {es['events']} engagement(s)" + (f", latched at exit ({es['source']}: {es['reason']})" if es["engaged"] else ""),
    "",
    "## Nodes",
    "",
    "| node | rate Hz | ticks | handled | errors | overruns | tick p50 / p95 / p99 ms | lateness p95 / max ms | handler p95 ms |",
    "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |",
  ]
  for name, node in report["nodes"].items():
    tick = node.get("tick_ms") or {}
    late = node.get("lateness_ms") or {}
    lines.append(
      f"| {name} | {node['rate_hz'] or '-'} | {node['ticks']} | {node['handled']} | {node['errors']} | {node['overruns']} | "
      f"{_ms(tick.get('p50'))} / {_ms(tick.get('p95'))} / {_ms(tick.get('p99'))} | "
      f"{_ms(late.get('p95'))} / {_ms(late.get('max'))} | {_ms(node['handler_ms'].get('p95'))} |"
    )
  lines += [
    "",
    "## Topics",
    "",
    "Latency is measured from each trace's root message to the moment this topic was published.",
    "",
    "| topic | count | recent Hz | dropped | latency p50 / p95 / p99 / max ms |",
    "| --- | ---: | ---: | ---: | --- |",
  ]
  for topic, stats in report["topics"].items():
    lat = stats["latency_ms"]
    lines.append(
      f"| {topic} | {stats['count']} | {stats['rate_hz']} | {stats['dropped']} | "
      f"{_ms(lat.get('p50'))} / {_ms(lat.get('p95'))} / {_ms(lat.get('p99'))} / {_ms(lat.get('max'))} |"
    )
  faults = report["faults"]
  lines += ["", "## Faults", ""]
  if faults["by_kind"]:
    lines.append("| kind | count |")
    lines.append("| --- | ---: |")
    lines += [f"| {kind} | {int(count)} |" for kind, count in faults["by_kind"].items()]
  else:
    lines.append("No faults recorded.")
  if report.get("artifacts"):
    lines += ["", "## Artifacts", ""]
    lines += [f"- {name}: `{path}`" for name, path in report["artifacts"].items()]
  return "\n".join(lines) + "\n"
