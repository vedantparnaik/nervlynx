from robot_core.live import LiveNode, LiveRuntime
from robot_core.live_config import build_live_runtime, validate_live_config
from robot_core.project import build_registry
from robot_core.runtime import SimulatedClock


def runtime(**kwargs) -> tuple[LiveRuntime, dict[str, list]]:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1, **kwargs)
  seen: dict[str, list] = {}
  for topic in ("range", "cmd", "frame"):
    rt.subscribe(f"probe.{topic}", topic, lambda msg, _t=topic: seen.setdefault(_t, []).append(msg.payload))
  return rt, seen


def test_latest_topic_delivers_only_the_newest_external_reading() -> None:
  rt, seen = runtime(latest_topics=["range"])
  rt.start()
  for n in range(5):
    rt.publish_external("range", "Range", {"n": n})
    rt.publish_external("cmd", "Cmd", {"n": n})
  rt.step()
  assert seen["range"] == [{"n": 4}]
  assert [p["n"] for p in seen["cmd"]] == [0, 1, 2, 3, 4]
  snap = rt.snapshot()
  assert snap["topics"]["range"]["conflated"] == 4 and snap["topics"]["range"]["latest_only"] is True
  assert snap["topics"]["cmd"]["conflated"] == 0 and snap["topics"]["cmd"]["latest_only"] is False
  assert snap["messages"]["conflated"] == 4


def test_latest_topic_supersedes_messages_already_queued() -> None:
  class Burst(LiveNode):
    def tick(self, ctx):
      return [("frame", "Frame", {"n": 1}), ("frame", "Frame", {"n": 2})]

  rt, seen = runtime(latest_topics=["frame"])
  rt.add_node("camera", Burst(), rate_hz=10)
  rt.start()
  rt.step()
  assert seen["frame"] == [{"n": 2}]
  assert rt.snapshot()["topics"]["frame"]["conflated"] == 1


def test_full_inbox_drops_and_reports_but_latest_topics_still_get_through() -> None:
  rt, seen = runtime(max_inbox_size=3, latest_topics=["range"])
  rt.start()
  accepted = [rt.publish_external("cmd", "Cmd", {"n": n}) for n in range(5)]
  assert accepted == [True, True, True, False, False]
  assert rt.publish_external("range", "Range", {"n": 9}) is True
  rt.step()
  assert [p["n"] for p in seen["cmd"]] == [0, 1, 2]
  assert seen["range"] == [{"n": 9}]
  snap = rt.snapshot()
  assert snap["topics"]["cmd"]["dropped"] == 2 and snap["messages"]["dropped"] == 2
  assert any(e.message == "inbox full: dropped 2 external message(s) on topic=cmd" for e in rt.fault_events)


def test_estop_requests_are_never_dropped_by_a_full_inbox() -> None:
  rt, _ = runtime(max_inbox_size=1)
  rt.start()
  rt.publish_external("cmd", "Cmd", {"n": 0})
  assert rt.publish_external("cmd", "Cmd", {"n": 1}) is False
  rt.request_estop("operator", source="test")
  rt.step()
  assert rt.estop_engaged


def test_config_sets_latest_topics_and_inbox_size() -> None:
  reg = build_registry()
  cfg = {
    "runtime": {"latest_topics": ["odom"], "max_inbox_size": 16},
    "nodes": [{"plugin": "scripted_drive", "rate_hz": 10, "params": {"steps": [{"linear": 0.2, "duration_s": 1.0}]}}],
  }
  assert validate_live_config(cfg, reg) == []
  rt = build_live_runtime(cfg, reg, clock=SimulatedClock())
  assert rt._latest_topics == frozenset({"odom"}) and rt._max_inbox == 16
  bad = {**cfg, "runtime": {"latest_topics": "odom", "max_inbox_size": 0}}
  issues = validate_live_config(bad, reg)
  assert "runtime.latest_topics must be a list of topic names" in issues
  assert "runtime.max_inbox_size must be a positive integer" in issues
