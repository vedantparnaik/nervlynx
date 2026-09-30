"""`@node`: write a NervLynx node as a plain Python function.

    from nervlynx import node

    @node(inputs=["range.front"], outputs="cmd.drive", rate_hz=20)
    def avoid(front, *, stop_m=0.3):
      if front["distance_m"] < stop_m:
        return {"linear": 0.0, "angular": 0.8}
      return {"linear": 0.25, "angular": 0.0}

Positional parameters receive the latest payload of each input, in order. Keyword-only
parameters are settings a config's `params:` can override. Parameters named `ctx` (the
NodeContext) and `state` (a dict kept between calls) are filled in by NervLynx.

With `rate_hz` the function runs at that rate using the newest input values; without it,
it runs on every incoming message. It waits until every input has arrived, and pauses
(publishing nothing) while any input is older than `max_age_s`, so a dead sensor makes
the drive deadman stop the robot instead of steering on stale data.

Return None to publish nothing, a dict for the single output, a number/bool/string
(published as {"value": x}), a dict keyed by topic when there are several outputs, or a
list of (topic, payload) pairs.

`@node` also accepts a `LiveNode` subclass, setting its inputs and rate.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from robot_core.live import LiveNode, NodeContext, Output
from robot_core.runtime import RuntimeMessage

NODE_ATTR = "__nervlynx_node__"
_INJECTED = ("ctx", "state")
_SCALARS = (int, float, bool, str)


@dataclass(frozen=True)
class NodeSpec:
  name: str
  inputs: tuple[str, ...]
  outputs: tuple[str, ...]
  rate_hz: float | None
  schema: str
  critical: bool
  max_age_s: float | None
  factory: Callable[..., LiveNode]


def _topics(value: Sequence[str] | str, what: str, owner: str) -> tuple[str, ...]:
  topics = (value,) if isinstance(value, str) else tuple(value)
  for topic in topics:
    if not isinstance(topic, str) or not topic.strip():
      raise TypeError(f"@node {owner}: {what} must be non-empty topic names")
  if len(set(topics)) != len(topics):
    raise TypeError(f"@node {owner}: {what} lists a topic twice")
  return topics


def _snake(name: str) -> str:
  return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class FunctionNode(LiveNode):
  """Runs a `@node` function inside the live executor."""

  def __init__(self, spec: NodeSpec, fn: Callable[..., Any], settings: dict[str, Any], injected: set[str]) -> None:
    self.spec = spec
    self.input_topics = spec.inputs
    self.rate_hz = spec.rate_hz
    self.critical = spec.critical
    self._fn = fn
    self._settings = settings
    self._injected = injected
    self._state: dict[str, Any] = {}
    self._latest: dict[str, dict[str, Any]] = {}
    self._stamp_ns: dict[str, int] = {}
    self._last_trace: str | None = None
    self._max_age_ns = int(spec.max_age_s * 1e9) if spec.max_age_s else None
    self._calls = 0
    self._stale: list[str] = []

  def on_message(self, msg: RuntimeMessage, ctx: NodeContext) -> Iterable[Output] | None:
    topic = msg.envelope.topic
    self._latest[topic] = msg.payload
    self._stamp_ns[topic] = ctx.now_ns
    self._last_trace = msg.envelope.trace_id
    if ctx.rate_hz is not None:
      return None
    return self._call(ctx)

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    for topic, schema, payload in self._call(ctx) or ():
      ctx.publish(topic, schema, payload, trace_id=self._last_trace)
    return None

  def _call(self, ctx: NodeContext) -> list[Output] | None:
    if any(topic not in self._latest for topic in self.spec.inputs):
      return None
    if self._max_age_ns is not None:
      now = ctx.now_ns
      self._stale = [t for t in self.spec.inputs if now - self._stamp_ns[t] > self._max_age_ns]
      if self._stale:
        return None
    args = [dict(self._latest[topic]) for topic in self.spec.inputs]
    kwargs = dict(self._settings)
    if "ctx" in self._injected:
      kwargs["ctx"] = ctx
    if "state" in self._injected:
      kwargs["state"] = self._state
    result = self._fn(*args, **kwargs)
    self._calls += 1
    return self._outputs(result)

  def _outputs(self, result: Any) -> list[Output] | None:
    if result is None:
      return None
    spec = self.spec
    single = spec.outputs[0] if len(spec.outputs) == 1 else None
    if not spec.outputs:
      raise TypeError(f"{spec.name} returned {type(result).__name__} but declares no outputs")
    if single is not None and isinstance(result, _SCALARS):
      return [(single, spec.schema, {"value": result})]
    if single is not None and isinstance(result, dict):
      return [(single, spec.schema, result)]
    if isinstance(result, dict):
      pairs: Iterable[Any] = result.items()
    elif isinstance(result, (list, tuple)):
      pairs = result
    else:
      raise TypeError(f"{spec.name} must return a dict, a number, a list of (topic, payload) pairs, or None; got {type(result).__name__}")
    out: list[Output] = []
    for item in pairs:
      if not isinstance(item, (list, tuple)) or len(item) not in (2, 3):
        raise TypeError(f"{spec.name} returned {item!r}; expected (topic, payload) or (topic, schema, payload)")
      topic, schema, payload = (item[0], spec.schema, item[1]) if len(item) == 2 else item
      if topic not in spec.outputs:
        raise ValueError(f"{spec.name} published to {topic!r}, which is not in its outputs {list(spec.outputs)}")
      if payload is None:
        continue
      out.append((topic, schema, payload if isinstance(payload, dict) else {"value": payload}))
    return out

  def status(self) -> dict[str, Any]:
    status: dict[str, Any] = {"calls": self._calls}
    missing = [topic for topic in self.spec.inputs if topic not in self._latest]
    if missing:
      status["waiting_for"] = missing
    if self._stale:
      status["stale_inputs"] = list(self._stale)
    return status


def _function_spec(
  fn: Callable[..., Any],
  *,
  name: str,
  inputs: tuple[str, ...],
  outputs: tuple[str, ...],
  rate_hz: float | None,
  schema: str,
  critical: bool,
  max_age_s: float | None,
) -> NodeSpec:
  positional: list[str] = []
  settings: dict[str, inspect.Parameter] = {}
  injected: set[str] = set()
  for param in inspect.signature(fn).parameters.values():
    if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
      raise TypeError(f"@node {name}: *args and **kwargs are not supported; name each input and setting")
    if param.name in _INJECTED:
      injected.add(param.name)
    elif param.kind is inspect.Parameter.KEYWORD_ONLY:
      settings[param.name] = param
    else:
      positional.append(param.name)
  if len(positional) != len(inputs):
    raise TypeError(
      f"@node {name}: the function takes {len(positional)} input parameter(s) ({', '.join(positional) or 'none'}) "
      f"but inputs lists {len(inputs)} topic(s) ({', '.join(inputs) or 'none'})"
    )

  def factory(**params: Any) -> LiveNode:
    unknown = sorted(set(params) - set(settings))
    if unknown:
      known = ", ".join(settings) or "none"
      raise TypeError(f"{name} got unknown params: {', '.join(unknown)} (settings: {known})")
    values = {key: param.default for key, param in settings.items() if param.default is not inspect.Parameter.empty}
    values.update(params)
    missing = sorted(set(settings) - set(values))
    if missing:
      raise TypeError(f"{name} needs params: {', '.join(missing)}")
    return FunctionNode(spec, fn, values, injected)

  factory.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
    [param.replace(kind=inspect.Parameter.KEYWORD_ONLY) for param in settings.values()]
  )
  factory.__name__ = name
  spec = NodeSpec(name, inputs, outputs, rate_hz, schema, critical, max_age_s, factory)
  return spec


def node(
  target: Any = None,
  *,
  inputs: Sequence[str] | str = (),
  outputs: Sequence[str] | str = (),
  rate_hz: float | None = None,
  name: str | None = None,
  schema: str = "Message",
  critical: bool = False,
  max_age_s: float | None = 1.0,
) -> Any:
  """Mark a function (or LiveNode subclass) as a node. See the module docstring."""

  def wrap(obj: Any) -> Any:
    node_name = name or (_snake(obj.__name__) if inspect.isclass(obj) else obj.__name__)
    input_topics = _topics(inputs, "inputs", node_name)
    output_topics = _topics(outputs, "outputs", node_name)
    if rate_hz is not None and (isinstance(rate_hz, bool) or not isinstance(rate_hz, (int, float)) or rate_hz <= 0):
      raise TypeError(f"@node {node_name}: rate_hz must be a positive number")
    if max_age_s is not None and (isinstance(max_age_s, bool) or not isinstance(max_age_s, (int, float)) or max_age_s <= 0):
      raise TypeError(f"@node {node_name}: max_age_s must be a positive number or None")
    if inspect.isclass(obj):
      if not issubclass(obj, LiveNode):
        raise TypeError(f"@node {node_name}: classes must subclass LiveNode")
      if input_topics:
        obj.input_topics = input_topics
      obj.rate_hz = rate_hz
      obj.critical = critical
      spec = NodeSpec(node_name, tuple(obj.input_topics), output_topics, rate_hz, schema, critical, max_age_s, obj)
    else:
      spec = _function_spec(
        obj,
        name=node_name,
        inputs=input_topics,
        outputs=output_topics,
        rate_hz=float(rate_hz) if rate_hz is not None else None,
        schema=schema,
        critical=critical,
        max_age_s=max_age_s,
      )
    setattr(obj, NODE_ATTR, spec)
    return obj

  if target is not None:
    return wrap(target)
  return wrap


def node_spec(obj: Any) -> NodeSpec | None:
  spec = getattr(obj, NODE_ATTR, None)
  return spec if isinstance(spec, NodeSpec) else None
