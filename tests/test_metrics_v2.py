import threading

from robot_core.metrics import Histogram, MetricsRegistry


def test_unlabelled_series_keep_legacy_text_format() -> None:
  reg = MetricsRegistry()
  reg.inc("nervlynx_events_total", 2)
  reg.set_gauge("nervlynx_queue_depth", 3)
  body = reg.render_prometheus()
  assert "# TYPE nervlynx_events_total counter\nnervlynx_events_total 2.0" in body
  assert "# TYPE nervlynx_queue_depth gauge\nnervlynx_queue_depth 3" in body


def test_labels_are_sorted_and_escaped() -> None:
  reg = MetricsRegistry()
  reg.inc("jobs_total", labels={"topic": 'a"b', "node": "x\\y\nz"})
  line = [l for l in reg.render_prometheus().splitlines() if l.startswith("jobs_total{")][0]
  assert line == 'jobs_total{node="x\\\\y\\nz",topic="a\\"b"} 1.0'


def test_help_text_is_rendered_when_described() -> None:
  reg = MetricsRegistry()
  reg.describe("jobs_total", "Jobs processed.")
  reg.inc("jobs_total")
  assert "# HELP jobs_total Jobs processed.\n# TYPE jobs_total counter" in reg.render_prometheus()


def test_histogram_exposition_is_cumulative() -> None:
  reg = MetricsRegistry()
  h = reg.histogram("lat_seconds", {"node": "n"}, buckets=(0.1, 1.0))
  for value in (0.05, 0.1, 0.5, 2.0):
    h.observe(value)
  body = reg.render_prometheus()
  assert 'lat_seconds_bucket{node="n",le="0.1"} 2' in body
  assert 'lat_seconds_bucket{node="n",le="1.0"} 3' in body
  assert 'lat_seconds_bucket{node="n",le="+Inf"} 4' in body
  assert 'lat_seconds_count{node="n"} 4' in body
  assert 'lat_seconds_sum{node="n"} 2.65' in body


def test_histogram_summary_quantiles() -> None:
  h = Histogram()
  for value in range(1, 101):
    h.observe(float(value))
  s = h.summary()
  assert s["count"] == 100
  assert s["p50"] == 50.0
  assert s["p99"] == 99.0
  assert s["min"] == 1.0 and s["max"] == 100.0
  assert s["recent_max"] == 100.0
  assert Histogram().summary()["p50"] is None


def test_reservoir_bounds_memory_but_keeps_lifetime_stats() -> None:
  h = Histogram(reservoir_size=64, recent_window=8)
  for value in range(10_000):
    h.observe(float(value))
  s = h.summary()
  assert s["count"] == 10_000
  assert s["max"] == 9999.0
  assert 2000 < s["p50"] < 8000
  assert s["recent_p50"] >= 9992


def test_value_lookup_and_snapshot() -> None:
  reg = MetricsRegistry()
  reg.inc("a_total", 3, labels={"k": "v"})
  reg.set_gauge("g", 1.5)
  reg.observe("h_seconds", 0.01)
  assert reg.value("a_total", {"k": "v"}) == 3.0
  assert reg.value("missing") is None
  snap = reg.snapshot()
  assert snap["counters"]["a_total"] == [{"labels": {"k": "v"}, "value": 3.0}]
  assert snap["gauges"]["g"][0]["value"] == 1.5
  assert snap["histograms"]["h_seconds"][0]["count"] == 1


def test_concurrent_updates_and_rendering_are_safe() -> None:
  reg = MetricsRegistry()
  errors: list[Exception] = []

  def work(idx: int) -> None:
    try:
      for n in range(5000):
        reg.inc("hits_total", labels={"worker": str(idx % 4)})
        reg.observe("work_seconds", n * 1e-6, labels={"worker": str(idx % 4)})
        if n % 500 == 0:
          reg.render_prometheus()
    except Exception as exc:  # pragma: no cover - surfaced by the assertion below
      errors.append(exc)

  threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
  for t in threads:
    t.start()
  for t in threads:
    t.join()
  assert errors == []
  total = sum(series["value"] for series in reg.snapshot()["counters"]["hits_total"])
  assert total == 8 * 5000
