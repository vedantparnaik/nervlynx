import threading
import time

import pytest

from robot_core.builtin_plugins import PerceptionNodePlugin, SyntheticSensorPlugin
from robot_core.dashboard import snapshot_runtime
from robot_core.live import ESTOP_TOPIC, LiveNode, LiveRuntime, LiveRuntimeError, PluginNodeAdapter, SensorSourceNode
from robot_core.runtime import SimulatedClock


class Source(LiveNode):
  def __init__(self, topic: str = "cmd") -> None:
    self.topic = topic

  def tick(self, ctx):
    return [(self.topic, "Cmd", {"v": 1})]


class Echo(LiveNode):
  input_topics = ("cmd",)

  def __init__(self) -> None:
    self.seen: list[str] = []

  def on_message(self, msg, ctx):
    self.seen.append(msg.envelope.trace_id)
    return [("ack", "Ack", {})]


class Flaky(LiveNode):
  input_topics = ("cmd",)

  def __init__(self) -> None:
    self.fail = True
    self.calls = 0

  def on_message(self, msg, ctx):
    self.calls += 1
    if self.fail:
      raise RuntimeError("sensor glitch")


class Recorder(LiveNode):
  """Remembers lifecycle calls so tests can check ordering and safety hooks."""

  def __init__(self, log: list[str], name: str, input_topics=()) -> None:
    self.log = log
    self.label = name
    self.input_topics = tuple(input_topics)
    self.hard_stops = 0

  def setup(self, ctx):
    self.log.append(f"setup:{self.label}")

  def on_message(self, msg, ctx):
    self.log.append(f"msg:{self.label}:{msg.envelope.topic}")

  def safe_stop(self, ctx):
    self.log.append(f"safe_stop:{self.label}")

  def hard_stop(self):
    self.hard_stops += 1

  def teardown(self, ctx):
    self.log.append(f"teardown:{self.label}")


def sim_runtime(**kwargs) -> LiveRuntime:
  return LiveRuntime(clock=SimulatedClock(), seed=1, **kwargs)


def test_ticks_follow_configured_rate_on_simulated_clock() -> None:
  rt = sim_runtime()
  rt.add_node("fast", Source("a"), rate_hz=50)
  rt.add_node("slow", Source("b"), rate_hz=10)
  rt.run(duration_s=2.0)
  snap = rt.snapshot()
  assert 100 <= snap["nodes"]["fast"]["ticks"] <= 101
  assert 20 <= snap["nodes"]["slow"]["ticks"] <= 21
  assert snap["topics"]["a"]["rate_hz"] == pytest.approx(50.0, rel=0.02)
  assert snap["nodes"]["fast"]["overruns"] == 0


def test_messages_from_handlers_continue_the_trace() -> None:
  rt = sim_runtime()
  rt.add_node("src", Source(), rate_hz=10)
  echo = rt.add_node("echo", Echo())
  seen: list = []
  rt.add_message_listener(seen.append)
  rt.run(duration_s=0.35)
  cmds = [m for m in seen if m.envelope.topic == "cmd"]
  acks = [m for m in seen if m.envelope.topic == "ack"]
  assert len(cmds) == len(acks) == len(echo.seen) == 4
  assert [m.envelope.trace_id for m in cmds] == [m.envelope.trace_id for m in acks]
  assert len({m.envelope.trace_id for m in cmds}) == 4
  assert rt.snapshot()["topics"]["ack"]["latency_ms"]["count"] == 4


def test_ctx_publish_continues_trace_in_handlers_and_starts_new_in_ticks() -> None:
  class Relay(LiveNode):
    input_topics = ("cmd",)

    def on_message(self, msg, ctx):
      ctx.publish("relayed", "R", {})

    def tick(self, ctx):
      ctx.publish("heartbeat", "H", {})

  rt = sim_runtime()
  rt.add_node("src", Source(), rate_hz=10)
  rt.add_node("relay", Relay(), rate_hz=10)
  seen: list = []
  rt.add_message_listener(seen.append)
  rt.run(duration_s=0.05)
  by_topic = {m.envelope.topic: m.envelope.trace_id for m in seen}
  assert by_topic["relayed"] == by_topic["cmd"]
  assert by_topic["heartbeat"] not in (by_topic["cmd"],)


def test_trace_ids_are_deterministic_for_a_seed() -> None:
  def run() -> list[str]:
    rt = sim_runtime()
    rt.add_node("src", Source(), rate_hz=10)
    ids: list[str] = []
    rt.add_message_listener(lambda m: ids.append(m.envelope.trace_id))
    rt.run(duration_s=0.5)
    return ids

  assert run() == run()


def test_handler_errors_trip_and_recover_the_circuit_breaker() -> None:
  rt = sim_runtime(breaker_threshold=3, breaker_cooldown_s=0.5)
  rt.add_node("src", Source(), rate_hz=20)
  flaky = rt.add_node("flaky", Flaky())
  rt.run(duration_s=1.0)
  node = rt.snapshot()["nodes"]["flaky"]
  assert node["errors"] >= 3 and node["breaker_trips"] >= 1 and node["skipped"] > 0
  assert node["breaker_open"] is True
  flaky.fail = False
  for _ in range(40):
    rt.clock.advance_ms(50)
    rt.step()
  node = rt.snapshot()["nodes"]["flaky"]
  assert node["breaker_open"] is False
  assert node["handled"] > 0
  assert "breaker_closed" in {f["kind"] for f in rt.snapshot(fault_limit=100)["faults"]}
  assert rt.metrics.value("nervlynx_node_breaker_open", {"node": "flaky"}) == 0


def test_critical_node_breaker_trip_latches_estop() -> None:
  log: list[str] = []
  rt = sim_runtime(breaker_threshold=2, breaker_cooldown_s=5.0)
  rt.add_node("src", Source(), rate_hz=20)
  rt.add_node("flaky", Flaky(), critical=True)
  rt.add_node("motor", Recorder(log, "motor"))
  rt.run(duration_s=0.5)
  snap = rt.snapshot()
  assert snap["estop"]["engaged"] is True
  assert snap["estop"]["source"] == "breaker"
  assert "safe_stop:motor" in log


def test_watchdog_latches_estop_and_blocks_clear_until_live() -> None:
  log: list[str] = []
  rt = sim_runtime(stale_after_s=0.2)
  source = Source()
  rt.add_node("src", source, rate_hz=20)
  rt.add_node("consumer", Recorder(log, "consumer", ["other"]), critical=True)
  rt.run(duration_s=0.5)
  snap = rt.snapshot()
  assert snap["nodes"]["consumer"]["stale"] is True
  assert snap["estop"] == {**snap["estop"], "engaged": True, "source": "watchdog"}
  assert rt.metrics.value("nervlynx_watchdog_faults_total", {"node": "consumer"}) == 1.0
  rt.request_estop_clear()
  rt.step()
  assert rt.estop_engaged is True
  assert "estop_clear_refused" in {f["kind"] for f in rt.snapshot()["faults"]}
  source.topic = "other"
  rt.clock.advance_ms(60)
  rt.step()
  rt.request_estop_clear()
  rt.step()
  assert rt.estop_engaged is False


def test_request_estop_hard_stops_immediately_then_latches() -> None:
  log: list[str] = []
  rt = sim_runtime()
  motor = rt.add_node("motor", Recorder(log, "motor"))
  seen: list = []
  rt.add_message_listener(seen.append)
  rt.start()
  worker = threading.Thread(target=rt.request_estop, args=("test",))
  worker.start()
  worker.join()
  assert motor.hard_stops == 1
  assert rt.estop_engaged is False
  rt.step()
  assert rt.estop_engaged is True
  assert "safe_stop:motor" in log
  rt.request_estop_clear()
  rt.step()
  events = [m.payload["engaged"] for m in seen if m.envelope.topic == ESTOP_TOPIC]
  assert events == [True, False]
  assert rt.metrics.value("nervlynx_estop_events_total", {"source": "api"}) == 1.0


def test_tick_overruns_are_counted() -> None:
  class Slow(LiveNode):
    def tick(self, ctx):
      ctx._runtime.clock.advance_ms(45)

  rt = sim_runtime()
  rt.add_node("slow", Slow(), rate_hz=50)
  rt.run(duration_s=1.0)
  node = rt.snapshot()["nodes"]["slow"]
  assert node["overruns"] > 0
  assert node["missed_ticks"] >= node["overruns"]
  assert node["lateness_ms"]["max"] >= 20.0


def test_backpressure_drops_are_counted_and_deduplicated() -> None:
  class Burst(LiveNode):
    input_topics = ("cmd",)

    def on_message(self, msg, ctx):
      return [("out", "O", {"i": i}) for i in range(5)]

  rt = sim_runtime(max_queue_size=2)
  rt.add_node("src", Source(), rate_hz=10)
  rt.add_node("burst", Burst())
  rt.run(duration_s=0.3)
  snap = rt.snapshot(fault_limit=100)
  assert snap["messages"]["dropped"] > 0
  assert snap["topics"]["out"]["dropped"] > 0
  assert sum(1 for f in snap["faults"] if f["kind"] == "backpressure") == 1


def test_message_loop_hits_hop_budget_without_freezing_simulated_time() -> None:
  class Ping(LiveNode):
    def __init__(self, inp: str, out: str) -> None:
      self.input_topics = (inp,)
      self.out = out

    def on_message(self, msg, ctx):
      return [(self.out, "P", {})]

  rt = sim_runtime(max_hops_per_step=50)
  rt.add_node("src", Source("a"), rate_hz=10)
  rt.add_node("ab", Ping("a", "b"))
  rt.add_node("ba", Ping("b", "a"))
  started = time.monotonic()
  rt.run(duration_s=0.5)
  assert time.monotonic() - started < 10
  assert rt.snapshot()["executor"]["hop_limit_events"] > 0


def test_simulated_run_requires_duration_when_nodes_tick() -> None:
  rt = sim_runtime()
  rt.add_node("src", Source(), rate_hz=10)
  with pytest.raises(LiveRuntimeError):
    rt.run()


def test_publish_external_is_delivered_on_next_step() -> None:
  rt = sim_runtime()
  echo = rt.add_node("echo", Echo())
  rt.start()
  t = threading.Thread(target=rt.publish_external, args=("cmd", "Cmd", {"v": 2}))
  t.start()
  t.join()
  assert echo.seen == []
  rt.step()
  assert len(echo.seen) == 1


def test_stop_ends_a_real_time_run_promptly() -> None:
  rt = LiveRuntime(stall_timeout_s=None)
  rt.add_node("src", Source(), rate_hz=100)
  worker = threading.Thread(target=rt.run)
  worker.start()
  time.sleep(0.2)
  rt.stop()
  worker.join(timeout=2.0)
  assert not worker.is_alive()
  assert rt.snapshot()["nodes"]["src"]["ticks"] > 5


def test_stall_guard_hard_stops_actuators_when_the_executor_blocks() -> None:
  log: list[str] = []

  class Hang(LiveNode):
    def __init__(self) -> None:
      self.ticks = 0

    def tick(self, ctx):
      self.ticks += 1
      if self.ticks == 3:
        time.sleep(0.4)

  rt = LiveRuntime(stall_timeout_s=0.1)
  motor = rt.add_node("motor", Recorder(log, "motor"))
  rt.add_node("hang", Hang(), rate_hz=50)
  rt.run(duration_s=0.8)
  snap = rt.snapshot(fault_limit=100)
  assert snap["executor"]["stalls"] == 1
  assert motor.hard_stops >= 1
  assert snap["estop"]["events"] == 1 and snap["estop"]["source"] == "stall_guard"
  assert "stall" in {f["kind"] for f in snap["faults"]}


def test_lifecycle_order_and_setup_failure_rollback() -> None:
  log: list[str] = []
  rt = sim_runtime()
  rt.add_node("a", Recorder(log, "a"))
  rt.add_node("b", Recorder(log, "b"))
  rt.start()
  rt.shutdown()
  assert log == ["setup:a", "setup:b", "safe_stop:a", "safe_stop:b", "teardown:b", "teardown:a"]

  class Broken(LiveNode):
    def setup(self, ctx):
      raise OSError("no such device")

  log.clear()
  rt = sim_runtime()
  rt.add_node("a", Recorder(log, "a"))
  rt.add_node("broken", Broken())
  with pytest.raises(LiveRuntimeError, match="broken"):
    rt.start()
  assert log == ["setup:a", "teardown:a"]


def test_rejects_duplicate_names_and_late_nodes() -> None:
  rt = sim_runtime()
  rt.add_node("a", Source(), rate_hz=1)
  with pytest.raises(LiveRuntimeError):
    rt.add_node("a", Source(), rate_hz=1)
  rt.start()
  with pytest.raises(LiveRuntimeError):
    rt.add_node("b", Source(), rate_hz=1)


def test_legacy_plugins_and_function_subscriptions_run_live() -> None:
  rt = sim_runtime()
  rt.add_node("camera", SensorSourceNode(SyntheticSensorPlugin(), "sensors.bundle", "SensorBundle"), rate_hz=10)
  rt.add_node("perception", PluginNodeAdapter(PerceptionNodePlugin()))
  scenes: list[dict] = []
  rt.subscribe("tap", "perception.scene", lambda msg: scenes.append(msg.payload))
  rt.run(duration_s=0.45)
  assert len(scenes) == 5
  assert scenes[0]["gps_fix"] is True
  legacy = snapshot_runtime(rt)
  assert legacy.subscriptions == {"sensors.bundle": 1, "perception.scene": 1}
  assert legacy.node_heartbeats_count == 3
