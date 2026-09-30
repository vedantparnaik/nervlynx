import pytest

from nervlynx import LiveNode, node
from robot_core.live import LiveRuntime
from robot_core.node_api import FunctionNode, node_spec
from robot_core.runtime import SimulatedClock


@node(inputs=["range.front"], outputs="cmd.drive", rate_hz=20)
def avoid(front, *, stop_m=0.3):
  if front["distance_m"] < stop_m:
    return {"linear": 0.0, "angular": 0.8}
  return {"linear": 0.25, "angular": 0.0}


@node(inputs="odom", outputs="odom.speed")
def speed(odom):
  return abs(odom["speed_mps"])


@node(inputs=["a", "b"], outputs="sum", rate_hz=10)
def add(a, b):
  return a["value"] + b["value"]


@node(outputs="blink", rate_hz=10)
def blink(*, state, ctx, period_ticks=2):
  state["n"] = state.get("n", 0) + 1
  return {"on": state["n"] % period_ticks == 0, "t_ns": ctx.now_ns}


def runtime_with(name: str, fn, **params) -> tuple[LiveRuntime, dict[str, list]]:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node(name, node_spec(fn).factory(**params))
  seen: dict[str, list] = {}
  for topic in node_spec(fn).inputs + node_spec(fn).outputs:
    rt.subscribe(f"probe.{topic}", topic, lambda msg, _t=topic: seen.setdefault(_t, []).append(msg))
  rt.start()
  return rt, seen


def advance(rt: LiveRuntime, seconds: float, step_ms: int = 50) -> None:
  for _ in range(int(round(seconds * 1000 / step_ms))):
    rt.clock.advance_ms(step_ms)
    rt.step()


def test_decorated_function_is_still_a_plain_function() -> None:
  assert avoid({"distance_m": 0.1}) == {"linear": 0.0, "angular": 0.8}
  assert avoid({"distance_m": 0.1}, stop_m=0.05) == {"linear": 0.25, "angular": 0.0}
  spec = node_spec(avoid)
  assert (spec.name, spec.inputs, spec.outputs, spec.rate_hz) == ("avoid", ("range.front",), ("cmd.drive",), 20.0)


def test_ticking_node_uses_latest_input_and_continues_its_trace() -> None:
  rt, seen = runtime_with("avoid", avoid)
  rt.publish_external("range.front", "Range", {"distance_m": 0.2})
  rt.step()
  assert seen["cmd.drive"][-1].payload == {"linear": 0.0, "angular": 0.8}
  assert seen["cmd.drive"][-1].envelope.trace_id == seen["range.front"][-1].envelope.trace_id
  rt.publish_external("range.front", "Range", {"distance_m": 2.0})
  advance(rt, 0.05)
  assert seen["cmd.drive"][-1].payload == {"linear": 0.25, "angular": 0.0}


def test_stale_input_pauses_the_node_so_the_deadman_can_stop_the_robot() -> None:
  rt, seen = runtime_with("avoid", avoid)
  rt.publish_external("range.front", "Range", {"distance_m": 2.0})
  advance(rt, 1.2)
  sent_while_fresh = len(seen["cmd.drive"])
  advance(rt, 1.0)
  assert len(seen["cmd.drive"]) == sent_while_fresh
  assert rt.nodes["avoid"].status()["stale_inputs"] == ["range.front"]


def test_node_waits_until_every_input_has_arrived() -> None:
  rt, seen = runtime_with("add", add)
  rt.publish_external("a", "Value", {"value": 1})
  advance(rt, 0.2)
  assert "sum" not in seen
  assert rt.nodes["add"].status()["waiting_for"] == ["b"]
  rt.publish_external("b", "Value", {"value": 2})
  advance(rt, 0.1)
  assert seen["sum"][-1].payload == {"value": 3}


def test_node_without_rate_reacts_to_each_message() -> None:
  rt, seen = runtime_with("speed", speed)
  rt.publish_external("odom", "Odometry", {"speed_mps": -0.4})
  rt.step()
  out = seen["odom.speed"][-1]
  assert out.payload == {"value": 0.4}
  assert out.envelope.trace_id == seen["odom"][-1].envelope.trace_id


def test_state_and_ctx_are_injected_and_settings_come_from_params() -> None:
  rt, seen = runtime_with("blink", blink, period_ticks=3)
  advance(rt, 0.3, step_ms=100)
  pattern = [msg.payload["on"] for msg in seen["blink"]]
  assert pattern[:3] == [False, False, True]
  assert seen["blink"][1].payload["t_ns"] > seen["blink"][0].payload["t_ns"]


def test_factory_checks_params_like_a_constructor() -> None:
  with pytest.raises(TypeError, match=r"unknown params: gain \(settings: stop_m\)"):
    node_spec(avoid).factory(gain=2)

  @node(outputs="x", rate_hz=1)
  def needs(*, gain):
    return gain

  with pytest.raises(TypeError, match="needs params: gain"):
    node_spec(needs).factory()
  assert isinstance(node_spec(needs).factory(gain=2), FunctionNode)


def test_decoration_errors_are_explained() -> None:
  with pytest.raises(TypeError, match=r"takes 0 input parameter\(s\) \(none\) but inputs lists 1 topic\(s\) \(a\)"):
    @node(inputs="a")
    def nothing():
      return None

  with pytest.raises(TypeError, match=r"\*args"):
    @node(inputs="a")
    def splat(*values):
      return None

  with pytest.raises(TypeError, match="rate_hz"):
    node(rate_hz=0)(lambda: None)


def test_bad_return_values_become_node_faults() -> None:
  @node(outputs=["left", "right"], rate_hz=10)
  def wrong_topic():
    return {"middle": {"x": 1}}

  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("wrong_topic", node_spec(wrong_topic).factory())
  rt.start()
  rt.step()
  assert any("not in its outputs" in event.message for event in rt.fault_events)


def test_multiple_outputs_by_topic() -> None:
  @node(outputs=["left", "right"], rate_hz=10)
  def split():
    return {"left": {"v": 1}, "right": 2}

  rt, seen = runtime_with("split", split)
  rt.step()
  assert seen["left"][-1].payload == {"v": 1}
  assert seen["right"][-1].payload == {"value": 2}


def test_node_decorates_live_node_classes() -> None:
  @node(inputs="cmd", rate_hz=5)
  class TickCounter(LiveNode):
    def __init__(self, *, start=0):
      self.count = start

    def tick(self, ctx):
      self.count += 1

  spec = node_spec(TickCounter)
  assert spec.name == "tick_counter" and spec.factory is TickCounter
  assert TickCounter.input_topics == ("cmd",)
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  counter = spec.factory(start=10)
  rt.add_node("counter", counter)
  rt.start()
  advance(rt, 1.0, step_ms=100)
  # 5 Hz over [0, 1.0] s: ticks fall due at 0.0, 0.2, ..., 1.0.
  assert counter.count == 10 + 6
