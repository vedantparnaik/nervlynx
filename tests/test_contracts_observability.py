from robot_core.contracts import TopicContract, check_contract_migration, default_contracts, validate_payload
from robot_core.observability import structured_event, timeline_by_trace, topic_latency_stats
from robot_core.runtime import Envelope, RuntimeMessage


def test_contract_validation_detects_missing_field() -> None:
  c = default_contracts()["mission.command"]
  issues = validate_payload(c, {"mode": "safe_slow_patrol"})
  assert any("missing required field" in i.message for i in issues)


def test_contract_migration_flags_required_field_removal() -> None:
  old = TopicContract("x.topic", "XSchema", 2, ("a", "b"))
  new = TopicContract("x.topic", "XSchema", 3, ("a",))
  issues = check_contract_migration(old, new)
  assert any("required fields removed" in i.message for i in issues)


def test_observability_timeline_and_stats() -> None:
  m1 = RuntimeMessage(
    envelope=Envelope("t.a", "n1", 1, 100, "trace-1", "S"),
    payload={"x": 1},
  )
  m2 = RuntimeMessage(
    envelope=Envelope("t.a", "n1", 2, 140, "trace-1", "S"),
    payload={"x": 2},
  )
  m3 = RuntimeMessage(
    envelope=Envelope("t.b", "n2", 1, 200, "trace-2", "S"),
    payload={"y": 1},
  )
  grouped = timeline_by_trace([m3, m2, m1])
  assert list(grouped["trace-1"][0].payload.keys()) == ["x"]
  stats = topic_latency_stats([m1, m2, m3])
  stat_map = {s.topic: s for s in stats}
  assert stat_map["t.a"].avg_delta_ms > 0
  evt = structured_event("fault", {"node": "planner"})
  assert evt["event_kind"] == "fault"
