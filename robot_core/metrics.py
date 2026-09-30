from __future__ import annotations

import bisect
import math
import random
import threading
from collections import deque
from threading import Thread
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Union

if TYPE_CHECKING:
  from http.server import HTTPServer

LabelKey = tuple[tuple[str, str], ...]
Labels = Union[Mapping[str, object], LabelKey, None]

# Seconds. Spans sub-100us handler calls up to multi-second stalls.
LATENCY_BUCKETS_S: tuple[float, ...] = (
  0.00005,
  0.0001,
  0.00025,
  0.0005,
  0.001,
  0.0025,
  0.005,
  0.01,
  0.025,
  0.05,
  0.1,
  0.25,
  0.5,
  1.0,
  2.5,
  5.0,
)


def label_key(labels: Labels) -> LabelKey:
  """Normalise labels to a sorted tuple. Tuples are assumed to be normalised already."""
  if not labels:
    return ()
  if isinstance(labels, tuple):
    return labels
  return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _escape_label_value(value: str) -> str:
  return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _series_name(name: str, key: LabelKey, extra: LabelKey = ()) -> str:
  pairs = key + extra
  if not pairs:
    return name
  body = ",".join(f'{k}="{_escape_label_value(v)}"' for k, v in pairs)
  return name + "{" + body + "}"


def _format_bound(bound: float) -> str:
  return "+Inf" if math.isinf(bound) else repr(float(bound))


def _quantile(sorted_values: list[float], q: float) -> float | None:
  if not sorted_values:
    return None
  idx = min(len(sorted_values) - 1, max(0, math.ceil(q * len(sorted_values)) - 1))
  return sorted_values[idx]


class Counter:
  __slots__ = ("_lock", "value")

  def __init__(self) -> None:
    self._lock = threading.Lock()
    self.value = 0.0

  def inc(self, amount: float = 1.0) -> None:
    with self._lock:
      self.value += amount


class Gauge:
  __slots__ = ("value",)

  def __init__(self) -> None:
    self.value: float = 0.0

  def set(self, value: float) -> None:
    self.value = value


class Histogram:
  """Prometheus-style cumulative buckets plus sample windows for quantiles.

  Quantiles (p50/p90/p95/p99) come from a uniform reservoir over the histogram's whole
  lifetime; the `recent_*` fields come from the last `recent_window` observations.
  """

  def __init__(
    self,
    buckets: Iterable[float] = LATENCY_BUCKETS_S,
    *,
    reservoir_size: int = 4096,
    recent_window: int = 256,
  ) -> None:
    self.buckets = tuple(sorted(float(b) for b in buckets))
    self._lock = threading.Lock()
    self._bucket_counts = [0] * (len(self.buckets) + 1)
    self._reservoir: list[float] = []
    self._reservoir_size = reservoir_size
    self._recent: deque[float] = deque(maxlen=recent_window)
    self._rng = random.Random(0x5EED)
    self.count = 0
    self.sum = 0.0
    self.min = math.inf
    self.max = -math.inf

  def observe(self, value: float) -> None:
    with self._lock:
      self._bucket_counts[bisect.bisect_left(self.buckets, value)] += 1
      self.count += 1
      self.sum += value
      if value < self.min:
        self.min = value
      if value > self.max:
        self.max = value
      self._recent.append(value)
      if len(self._reservoir) < self._reservoir_size:
        self._reservoir.append(value)
      else:
        slot = self._rng.randrange(self.count)
        if slot < self._reservoir_size:
          self._reservoir[slot] = value

  def prometheus_snapshot(self) -> tuple[list[tuple[float, int]], float, int]:
    with self._lock:
      counts = list(self._bucket_counts)
      total, count = self.sum, self.count
    cumulative: list[tuple[float, int]] = []
    running = 0
    for bound, n in zip(self.buckets + (math.inf,), counts):
      running += n
      cumulative.append((bound, running))
    return cumulative, total, count

  def summary(self) -> dict[str, Any]:
    with self._lock:
      sample = sorted(self._reservoir)
      recent = sorted(self._recent)
      count, total, lo, hi = self.count, self.sum, self.min, self.max
    if count == 0:
      return {
        "count": 0,
        "mean": None,
        "min": None,
        "max": None,
        "p50": None,
        "p90": None,
        "p95": None,
        "p99": None,
        "recent_p50": None,
        "recent_p95": None,
        "recent_max": None,
      }
    return {
      "count": count,
      "mean": total / count,
      "min": lo,
      "max": hi,
      "p50": _quantile(sample, 0.50),
      "p90": _quantile(sample, 0.90),
      "p95": _quantile(sample, 0.95),
      "p99": _quantile(sample, 0.99),
      "recent_p50": _quantile(recent, 0.50),
      "recent_p95": _quantile(recent, 0.95),
      "recent_max": recent[-1] if recent else None,
    }


class MetricsRegistry:
  """Thread-safe registry of labelled counters, gauges, and histograms.

  `inc`, `set_gauge`, and `observe` look series up by name and labels. Hot paths can keep
  the object returned by `counter`/`gauge`/`histogram` and update it directly.
  """

  def __init__(self) -> None:
    self._lock = threading.RLock()
    self._counters: dict[str, dict[LabelKey, Counter]] = {}
    self._gauges: dict[str, dict[LabelKey, Gauge]] = {}
    self._histograms: dict[str, dict[LabelKey, Histogram]] = {}
    self._help: dict[str, str] = {}

  def describe(self, name: str, help_text: str) -> None:
    with self._lock:
      self._help[name] = help_text

  def counter(self, name: str, labels: Labels = None) -> Counter:
    key = label_key(labels)
    with self._lock:
      family = self._counters.setdefault(name, {})
      series = family.get(key)
      if series is None:
        series = family[key] = Counter()
      return series

  def gauge(self, name: str, labels: Labels = None) -> Gauge:
    key = label_key(labels)
    with self._lock:
      family = self._gauges.setdefault(name, {})
      series = family.get(key)
      if series is None:
        series = family[key] = Gauge()
      return series

  def histogram(self, name: str, labels: Labels = None, buckets: Iterable[float] = LATENCY_BUCKETS_S) -> Histogram:
    key = label_key(labels)
    with self._lock:
      family = self._histograms.setdefault(name, {})
      series = family.get(key)
      if series is None:
        series = family[key] = Histogram(buckets)
      return series

  def inc(self, name: str, amount: float = 1.0, labels: Labels = None) -> None:
    self.counter(name, labels).inc(amount)

  def set_gauge(self, name: str, value: float, labels: Labels = None) -> None:
    self.gauge(name, labels).set(value)

  def observe(self, name: str, value: float, labels: Labels = None) -> None:
    self.histogram(name, labels).observe(value)

  def value(self, name: str, labels: Labels = None) -> float | None:
    """Current value of a counter or gauge series, or None if it does not exist."""
    key = label_key(labels)
    with self._lock:
      for family in (self._counters.get(name), self._gauges.get(name)):
        if family and key in family:
          return family[key].value
    return None

  def _families(self) -> tuple[dict, dict, dict, dict[str, str]]:
    with self._lock:
      return (
        {n: dict(f) for n, f in self._counters.items()},
        {n: dict(f) for n, f in self._gauges.items()},
        {n: dict(f) for n, f in self._histograms.items()},
        dict(self._help),
      )

  def render_prometheus(self) -> str:
    counters, gauges, histograms, help_texts = self._families()
    lines: list[str] = []

    def header(name: str, kind: str) -> None:
      if name in help_texts:
        lines.append(f"# HELP {name} {help_texts[name]}")
      lines.append(f"# TYPE {name} {kind}")

    for name in sorted(counters):
      header(name, "counter")
      for key in sorted(counters[name]):
        lines.append(f"{_series_name(name, key)} {counters[name][key].value}")
    for name in sorted(gauges):
      header(name, "gauge")
      for key in sorted(gauges[name]):
        lines.append(f"{_series_name(name, key)} {gauges[name][key].value}")
    for name in sorted(histograms):
      header(name, "histogram")
      for key in sorted(histograms[name]):
        cumulative, total, count = histograms[name][key].prometheus_snapshot()
        for bound, running in cumulative:
          lines.append(f"{_series_name(name + '_bucket', key, (('le', _format_bound(bound)),))} {running}")
        lines.append(f"{_series_name(name + '_sum', key)} {total}")
        lines.append(f"{_series_name(name + '_count', key)} {count}")
    return "\n".join(lines) + ("\n" if lines else "")

  def snapshot(self) -> dict[str, Any]:
    """JSON-friendly view of every series (histograms reported as summaries)."""
    counters, gauges, histograms, _ = self._families()
    return {
      "counters": {
        name: [{"labels": dict(key), "value": series.value} for key, series in sorted(family.items())]
        for name, family in sorted(counters.items())
      },
      "gauges": {
        name: [{"labels": dict(key), "value": series.value} for key, series in sorted(family.items())]
        for name, family in sorted(gauges.items())
      },
      "histograms": {
        name: [{"labels": dict(key), **series.summary()} for key, series in sorted(family.items())]
        for name, family in sorted(histograms.items())
      },
    }


def serve_metrics(registry: MetricsRegistry, host: str = "0.0.0.0", port: int = 9108) -> HTTPServer:
  from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

  class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
      if self.path != "/metrics":
        self.send_response(404)
        self.end_headers()
        return
      body = registry.render_prometheus().encode("utf-8")
      self.send_response(200)
      self.send_header("Content-Type", "text/plain; version=0.0.4")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
      return

  server = ThreadingHTTPServer((host, port), Handler)
  server.daemon_threads = True
  thread = Thread(target=server.serve_forever, daemon=True)
  thread.start()
  return server
