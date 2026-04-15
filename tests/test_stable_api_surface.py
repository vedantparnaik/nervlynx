from robot_core import stable_api


def test_stable_api_surface_is_explicit_and_locked() -> None:
  expected = {
    "Envelope",
    "RuntimeMessage",
    "PipelineRuntime",
    "SystemClock",
    "SimulatedClock",
    "HealthWatchdog",
    "PluginRegistry",
    "SensorPlugin",
    "NodePlugin",
    "write_jsonl",
    "read_jsonl",
    "MetricsRegistry",
    "serve_metrics",
  }
  assert set(stable_api.__all__) == expected
  for symbol in expected:
    assert hasattr(stable_api, symbol)
