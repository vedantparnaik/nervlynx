from robot_core.examples import build_reference_runtime
from robot_core.watchdog import HealthWatchdog


def test_runtime_routes_messages_and_preserves_trace() -> None:
  rt = build_reference_runtime()
  seed = rt.publish(
    topic="sensors.raw",
    source="sensor_adapter",
    schema="RawSensors",
    payload={"camera_count": 3, "gps_fix": True},
  )
  trace = rt.run_once(seed)
  assert [m.envelope.topic for m in trace] == [
    "sensors.raw",
    "perception.state",
    "control.intent",
  ]
  assert len({m.envelope.trace_id for m in trace}) == 1


def test_watchdog_reports_stale_nodes() -> None:
  rt = build_reference_runtime()
  seed = rt.publish(
    topic="sensors.raw",
    source="sensor_adapter",
    schema="RawSensors",
    payload={"camera_count": 1, "gps_fix": False},
  )
  _ = rt.run_once(seed)
  heartbeats = rt.node_heartbeats_ns
  assert "fusion" in heartbeats
  wd = HealthWatchdog(stale_after_s=0.1)
  future_ns = max(heartbeats.values()) + int(0.2e9)
  faults = wd.check(heartbeats, now_ns=future_ns)
  assert any(f.node_name == "fusion" for f in faults)
