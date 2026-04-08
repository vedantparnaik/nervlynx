import asyncio

from robot_core.runtime import AsyncPipelineRuntime, PipelineRuntime, SimulatedClock


def test_priority_order_prefers_lower_priority_value() -> None:
  rt = PipelineRuntime(topic_priority={"high": 1, "low": 10})

  def handler(_msg):
    return [
      ("low", "Low", {"v": 1}),
      ("high", "High", {"v": 2}),
    ]

  rt.subscribe("n1", "seed", handler)
  seed = rt.publish(topic="seed", source="src", schema="Seed", payload={})
  trace = rt.run_once(seed)
  topics = [m.envelope.topic for m in trace]
  assert topics[:3] == ["seed", "high", "low"]


def test_backpressure_fault_is_recorded() -> None:
  rt = PipelineRuntime(max_queue_size=1)

  def handler(_msg):
    return [("a", "A", {}), ("b", "B", {})]

  rt.subscribe("n1", "seed", handler)
  seed = rt.publish(topic="seed", source="src", schema="Seed", payload={})
  _ = rt.run_once(seed)
  assert any("backpressure drop" in f for f in rt.faults)


def test_simulated_clock_timestamp_control() -> None:
  clock = SimulatedClock(start_ns=1000)
  rt = PipelineRuntime(clock=clock)
  m1 = rt.publish(topic="a", source="s", schema="A", payload={})
  clock.advance_ms(5)
  m2 = rt.publish(topic="a", source="s", schema="A", payload={})
  assert m2.envelope.monotonic_time_ns - m1.envelope.monotonic_time_ns == 5_000_000


def test_async_runtime_executes() -> None:
  rt = AsyncPipelineRuntime()

  async def h(_msg):
    return [("done", "Done", {"ok": True})]

  rt.subscribe_async("async_node", "seed", h)
  seed = rt.publish(topic="seed", source="src", schema="Seed", payload={})
  trace = asyncio.run(rt.run_once_async(seed))
  assert [m.envelope.topic for m in trace] == ["seed", "done"]
