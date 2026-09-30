"""Artifacts for live runs: streaming trace and fault logs, plus the end-of-run report."""

from __future__ import annotations

import json
import platform
import threading
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Iterable, TextIO

from robot_core.live import FaultEvent, LiveRuntime
from robot_core.recorder import encode_message
from robot_core.runtime import RuntimeMessage


def package_version() -> str:
  try:
    return version("nervlynx")
  except PackageNotFoundError:
    return "nervlynx-unknown"


class TraceRecorder:
  """Appends every published message to a JSONL trace readable by `robot-core replay`."""

  def __init__(
    self,
    path: str | Path,
    *,
    exclude_topics: Iterable[str] = (),
    max_messages: int | None = 2_000_000,
    flush_every: int = 256,
  ) -> None:
    self.path = Path(path)
    self.path.parent.mkdir(parents=True, exist_ok=True)
    self._file: TextIO | None = self.path.open("w", encoding="utf-8")
    self._exclude = frozenset(exclude_topics)
    self._max = max_messages
    self._flush_every = max(1, flush_every)
    self.written = 0
    self.skipped = 0

  def __call__(self, msg: RuntimeMessage) -> None:
    if self._file is None or msg.envelope.topic in self._exclude:
      return
    if self._max is not None and self.written >= self._max:
      self.skipped += 1
      return
    self._file.write(json.dumps(encode_message(msg), separators=(",", ":"), default=str) + "\n")
    self.written += 1
    if self.written % self._flush_every == 0:
      self._file.flush()

  def close(self) -> None:
    if self._file is not None:
      self._file.close()
      self._file = None


class FaultLog:
  """Streams structured faults to JSONL as they happen (thread-safe)."""

  def __init__(self, path: str | Path) -> None:
    self.path = Path(path)
    self.path.parent.mkdir(parents=True, exist_ok=True)
    self._lock = threading.Lock()
    self._file: TextIO | None = self.path.open("w", encoding="utf-8")
    self.count = 0

  def __call__(self, event: FaultEvent) -> None:
    with self._lock:
      if self._file is None:
        return
      self._file.write(json.dumps(event.to_dict()) + "\n")
      self._file.flush()
      self.count += 1

  def close(self) -> None:
    with self._lock:
      if self._file is not None:
        self._file.close()
        self._file = None


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
