"""Continuous (live) execution of NervLynx graphs.

`PipelineRuntime.run_once` pushes one seed message through a graph and returns. A robot
needs the graph to keep running: sensors sampled at fixed rates, control loops ticking,
commands flowing to actuators, and a way to stop everything safely. `LiveRuntime` provides
that on top of the same envelopes, topics, and plugins.

Threading model: every node callback runs on the executor thread (the one calling `run()`
or `step()`), so nodes never need locks. Other threads interact only through
`publish_external()`, `request_estop()`, `request_estop_clear()`, `call_node()`, and
`snapshot()`.
"""

from __future__ import annotations

import heapq
import json
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from robot_core.metrics import Counter, Histogram, MetricsRegistry, label_key
from robot_core.runtime import Clock, PipelineRuntime, RuntimeMessage, SystemClock

Output = tuple[str, str, dict[str, Any]]
Handler = Callable[[RuntimeMessage], Iterable[Output] | None]

ESTOP_TOPIC = "safety.estop"
_RATE_WINDOW_NS = 2_000_000_000
_FAULT_COOLDOWN_NS = 1_000_000_000
_TRACE_ORIGIN_CAPACITY = 8192
_PAYLOAD_PREVIEW_BYTES = 2048


class LiveRuntimeError(RuntimeError):
  pass


@dataclass(frozen=True)
class FaultEvent:
  time_ns: int
  kind: str
  severity: str
  message: str
  node: str | None = None

  def to_dict(self) -> dict[str, Any]:
    return {
      "time_ns": self.time_ns,
      "kind": self.kind,
      "severity": self.severity,
      "message": self.message,
      "node": self.node,
    }


class LiveNode:
  """Base class for nodes run by `LiveRuntime`. Override only what the node needs.

  Constructors must not touch hardware; acquire devices in `setup` so configs can be
  validated (and nodes instantiated) on any machine.
  """

  input_topics: tuple[str, ...] = ()

  def setup(self, ctx: NodeContext) -> None:
    return None

  def on_message(self, msg: RuntimeMessage, ctx: NodeContext) -> Iterable[Output] | None:
    return None

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    return None

  def safe_stop(self, ctx: NodeContext) -> None:
    """Drive outputs to a safe state. Called on e-stop, breaker trip, and shutdown."""
    return None

  def hard_stop(self) -> None:
    """Emergency stop callable from any thread. Must be fast, idempotent, and thread-safe."""
    return None

  def teardown(self, ctx: NodeContext) -> None:
    return None

  def status(self) -> dict[str, Any]:
    """Extra state surfaced in `/stats` and run reports."""
    return {}

  def calibrate(self, action: str, args: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    """One step of the dashboard's calibration wizard, run on the executor thread.

    `describe` returns what the wizard should show; other actions change settings live
    (and may move actuators, so they must honour the e-stop). Raise ValueError to reject
    an action with a message for the operator.
    """
    raise ValueError(f"{type(self).__name__} has nothing to calibrate")

  def calibration(self) -> dict[str, Any] | None:
    """Settings changed by `calibrate`, as a `params` patch for calibration.yaml."""
    return None


def supports_calibration(node: LiveNode) -> bool:
  return type(node).calibrate is not LiveNode.calibrate


class NodeContext:
  """Handle a node uses to publish, record faults, and read runtime state."""

  def __init__(self, runtime: LiveRuntime, name: str) -> None:
    self.name = name
    self._runtime = runtime
    self._current_trace: str | None = None

  @property
  def now_ns(self) -> int:
    return self._runtime.clock.monotonic_ns()

  @property
  def simulated(self) -> bool:
    return self._runtime.clock.simulated

  @property
  def metrics(self) -> MetricsRegistry:
    return self._runtime.metrics

  @property
  def estop_engaged(self) -> bool:
    return self._runtime.estop_engaged

  @property
  def rate_hz(self) -> float | None:
    """This node's tick rate, or None when it only reacts to messages."""
    slot = self._runtime._slots.get(self.name)
    return 1e9 / slot.period_ns if slot is not None and slot.period_ns else None

  def publish(self, topic: str, schema: str, payload: dict[str, Any], *, trace_id: str | None = None) -> RuntimeMessage:
    """Publish now. Inside `on_message` the current trace is continued unless `trace_id` is given."""
    return self._runtime._emit(self.name, topic, schema, payload, trace_id or self._current_trace)

  def fault(self, message: str, *, severity: str = "warning", kind: str = "node") -> None:
    self._runtime._record_fault(kind, message, node=self.name, severity=severity, dedupe_key=(kind, self.name, message))

  def engage_estop(self, reason: str) -> None:
    self._runtime._engage_estop(reason, source=self.name)


class _CallbackNode(LiveNode):
  """Holds plain handler functions registered through `subscribe()`."""


class PluginNodeAdapter(LiveNode):
  """Runs a one-shot `NodePlugin` (`handle(msg) -> outputs`) inside the live executor."""

  def __init__(self, plugin: Any, input_topics: Iterable[str] | None = None) -> None:
    self.plugin = plugin
    topics = input_topics if input_topics is not None else (getattr(plugin, "input_topics", None) or ())
    self.input_topics = tuple(topics)

  def on_message(self, msg: RuntimeMessage, ctx: NodeContext) -> Iterable[Output] | None:
    return self.plugin.handle(msg)


class SensorSourceNode(LiveNode):
  """Samples a `SensorPlugin.read()` on every tick and publishes the reading."""

  def __init__(self, plugin: Any, topic: str, schema: str) -> None:
    self.plugin = plugin
    self.topic = topic
    self.schema = schema

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    return [(self.topic, self.schema, dict(self.plugin.read()))]


@dataclass
class _Slot:
  name: str
  node: LiveNode
  ctx: NodeContext
  topics: tuple[str, ...]
  period_ns: int | None
  critical: bool
  stale_after_ns: int | None
  handlers: dict[str, Handler] = field(default_factory=dict)
  next_tick_ns: int = 0
  ticks: int = 0
  handled: int = 0
  errors: int = 0
  consecutive_errors: int = 0
  breaker_open_until_ns: int = 0
  breaker_trips: int = 0
  overruns: int = 0
  missed_ticks: int = 0
  skipped: int = 0
  last_ok_ns: int | None = None
  stale: bool = False
  last_error: str | None = None
  tick_hist: Histogram | None = None
  lateness_hist: Histogram | None = None
  handler_hist: Histogram | None = None
  delivered_counters: dict[str, Counter] = field(default_factory=dict)


@dataclass
class _TopicStats:
  count: int = 0
  dropped: int = 0
  conflated: int = 0
  recent: deque = field(default_factory=lambda: deque(maxlen=256))
  latency: Histogram | None = None
  published_counters: dict[str, Counter] = field(default_factory=dict)
  last_payload: dict[str, Any] | None = None
  last_source: str | None = None

  def rate_hz(self, now_ns: int) -> float:
    """Messages per second over the last couple of seconds (0 once the topic goes quiet)."""
    stamps = self.recent
    if len(stamps) < 2 or now_ns - stamps[-1] > _RATE_WINDOW_NS:
      return 0.0
    cutoff = now_ns - _RATE_WINDOW_NS
    in_window = [t for t in stamps if t >= cutoff]
    if len(in_window) < 2:
      return 0.0
    span = in_window[-1] - in_window[0]
    return (len(in_window) - 1) * 1e9 / span if span > 0 else 0.0


def _summary_ms(hist: Histogram | None) -> dict[str, Any]:
  if hist is None:
    return {"count": 0}
  s = hist.summary()
  out: dict[str, Any] = {"count": s["count"]}
  for key in ("mean", "min", "max", "p50", "p90", "p95", "p99", "recent_p50", "recent_p95", "recent_max"):
    value = s[key]
    out[key] = None if value is None else round(value * 1000.0, 4)
  return out


def _payload_preview(payload: dict[str, Any] | None) -> Any:
  if payload is None:
    return None
  try:
    text = json.dumps(payload, default=str)
  except (TypeError, ValueError):
    return "<unserialisable>"
  if len(text) > _PAYLOAD_PREVIEW_BYTES:
    return f"<{len(text)} bytes>"
  return json.loads(text)


class _StallGuard(threading.Thread):
  """Stops actuators from outside the executor thread if the executor stops stepping."""

  def __init__(self, runtime: LiveRuntime, timeout_ns: int) -> None:
    super().__init__(name="nervlynx-stall-guard", daemon=True)
    self._runtime = runtime
    self._timeout_ns = timeout_ns
    self._poll_s = min(0.05, timeout_ns / 4e9)
    self._halt = threading.Event()
    self._tripped = False

  def run(self) -> None:
    while not self._halt.wait(self._poll_s):
      age = time.monotonic_ns() - self._runtime._last_step_wall_ns
      if age > self._timeout_ns:
        if not self._tripped:
          self._tripped = True
          self._runtime._on_stall(age)
      else:
        self._tripped = False

  def stop(self) -> None:
    self._halt.set()


class LiveRuntime(PipelineRuntime):
  """Continuously running, instrumented executor for node graphs."""

  def __init__(
    self,
    *,
    name: str = "live",
    clock: Clock | None = None,
    metrics: MetricsRegistry | None = None,
    max_queue_size: int = 4096,
    topic_priority: dict[str, int] | None = None,
    max_hops_per_step: int = 4096,
    breaker_threshold: int = 5,
    breaker_cooldown_s: float = 2.0,
    stale_after_s: float | None = None,
    estop_on_stale: bool = True,
    stall_timeout_s: float | None = 0.5,
    max_idle_sleep_s: float = 0.05,
    fault_history: int = 512,
    seed: int | None = None,
    latest_topics: Iterable[str] = (),
    max_inbox_size: int = 4096,
  ) -> None:
    """`latest_topics` deliver only their newest pending message (sensor readings, frames);
    other topics queue every message. `max_inbox_size` bounds messages waiting from other
    threads; beyond it `publish_external` drops them (e-stop requests are never dropped)."""
    super().__init__(max_queue_size=max_queue_size, topic_priority=topic_priority, clock=clock or SystemClock())
    self.name = name
    self.metrics = metrics if metrics is not None else MetricsRegistry()
    self._faults = deque(maxlen=fault_history)  # type: ignore[assignment]
    self._fault_events: deque[FaultEvent] = deque(maxlen=fault_history)
    self._fault_lock = threading.Lock()
    self._fault_last_ns: dict[Any, int] = {}
    self._fault_listeners: list[Callable[[FaultEvent], None]] = []
    self._message_listeners: list[Callable[[RuntimeMessage], None]] = []
    self._max_hops_per_step = max_hops_per_step
    self._hops_left = max_hops_per_step
    self._hop_limit_hit = False
    self._breaker_threshold = breaker_threshold
    self._breaker_cooldown_ns = int(breaker_cooldown_s * 1e9)
    self._default_stale_ns = int(stale_after_s * 1e9) if stale_after_s else None
    self._estop_on_stale = estop_on_stale
    self._stall_timeout_ns = int(stall_timeout_s * 1e9) if stall_timeout_s else None
    self._max_idle_sleep_ns = int(max_idle_sleep_s * 1e9)
    if self._stall_timeout_ns:
      # The executor must wake well inside the stall window or the guard would misfire.
      self._max_idle_sleep_ns = min(self._max_idle_sleep_ns, self._stall_timeout_ns // 4)
    self._stop_requested = False
    self._slots: dict[str, _Slot] = {}
    self._tick_slots: list[_Slot] = []
    self._routes: dict[str, list[tuple[_Slot, Handler]]] = {}
    self._topics: dict[str, _TopicStats] = {}
    self._trace_origin: dict[str, int] = {}
    self._rng = random.Random(seed if seed is not None else random.SystemRandom().getrandbits(64))
    self._inbox: deque[tuple[str, Any]] = deque()
    self._inbox_lock = threading.Lock()
    self._latest_topics = frozenset(latest_topics)
    self._latest_enqueued: dict[str, int] = {}
    self._inbox_latest: dict[str, tuple[str, str, dict[str, Any], str]] = {}
    self._inbox_msgs = 0
    self._max_inbox = max_inbox_size
    self._inbox_conflated: dict[str, int] = {}
    self._inbox_overflow: dict[str, int] = {}
    self._conflated = 0
    self._wake = threading.Event()
    self._state_lock = threading.RLock()
    self._estop = False
    self._estop_reason = ""
    self._estop_source = ""
    self._estop_since_ns: int | None = None
    self._estop_events = 0
    self._stalls = 0
    self._started = False
    self._stopped = False
    self._started_ns: int | None = None
    self._started_wall = 0.0
    self._steps = 0
    self._published = 0
    self._delivered = 0
    self._dropped = 0
    self._hop_limit_events = 0
    self._queue_depth_max = 0
    self._last_step_wall_ns = time.monotonic_ns()
    self._last_gauge_ns = 0
    self._describe_metrics()
    self._m_steps = self.metrics.counter("nervlynx_steps_total")
    self._m_queue = self.metrics.gauge("nervlynx_queue_depth")
    self._m_uptime = self.metrics.gauge("nervlynx_uptime_seconds")
    self._m_estop = self.metrics.gauge("nervlynx_estop_engaged")
    self._m_hops = self.metrics.counter("nervlynx_hop_limit_events_total")

  # ------------------------------------------------------------------ graph building

  @property
  def clock(self) -> Clock:
    return self._clock

  @property
  def started(self) -> bool:
    return self._started

  @property
  def nodes(self) -> dict[str, LiveNode]:
    return {name: slot.node for name, slot in self._slots.items()}

  def add_node(
    self,
    name: str,
    node: LiveNode,
    *,
    input_topics: Iterable[str] | None = None,
    rate_hz: float | None = None,
    critical: bool = False,
    stale_after_s: float | None = None,
  ) -> LiveNode:
    if self._started:
      raise LiveRuntimeError("nodes must be added before the runtime starts")
    if name in self._slots:
      raise LiveRuntimeError(f"duplicate node name: {name}")
    if rate_hz is None:
      rate_hz = getattr(node, "rate_hz", None)
    if rate_hz is not None and rate_hz <= 0:
      raise LiveRuntimeError(f"node {name}: rate_hz must be > 0")
    topics = tuple(input_topics) if input_topics is not None else tuple(node.input_topics)
    stale_ns = int(stale_after_s * 1e9) if stale_after_s else (self._default_stale_ns if critical else None)
    slot = _Slot(
      name=name,
      node=node,
      ctx=NodeContext(self, name),
      topics=topics,
      period_ns=int(1e9 / rate_hz) if rate_hz else None,
      critical=critical,
      stale_after_ns=stale_ns,
    )
    labels = label_key({"node": name})
    slot.handler_hist = self.metrics.histogram("nervlynx_handler_seconds", labels)
    if slot.period_ns:
      slot.tick_hist = self.metrics.histogram("nervlynx_tick_seconds", labels)
      slot.lateness_hist = self.metrics.histogram("nervlynx_tick_lateness_seconds", labels)
      self._tick_slots.append(slot)
    self._slots[name] = slot
    for topic in topics:
      self._route(slot, topic, lambda msg, _node=node, _ctx=slot.ctx: _node.on_message(msg, _ctx))
    return node

  def subscribe(self, node_name: str, topic: str, handler: Handler) -> None:  # type: ignore[override]
    slot = self._slots.get(node_name)
    if slot is None:
      self.add_node(node_name, _CallbackNode(), input_topics=())
      slot = self._slots[node_name]
    slot.topics = tuple(dict.fromkeys(slot.topics + (topic,)))
    self._route(slot, topic, handler)

  def _route(self, slot: _Slot, topic: str, handler: Handler) -> None:
    slot.handlers[topic] = handler
    slot.delivered_counters[topic] = self.metrics.counter(
      "nervlynx_messages_delivered_total", label_key({"node": slot.name, "topic": topic})
    )
    self._routes.setdefault(topic, []).append((slot, handler))
    self._subscriptions[topic].append((slot.name, handler))

  def add_message_listener(self, listener: Callable[[RuntimeMessage], None]) -> None:
    """Called on the executor thread for every published message (e.g. a trace recorder)."""
    self._message_listeners.append(listener)

  def add_fault_listener(self, listener: Callable[[FaultEvent], None]) -> None:
    """Called for every recorded fault. May run on the stall-guard thread."""
    self._fault_listeners.append(listener)

  # ------------------------------------------------------------------ thread-safe control

  def publish_external(self, topic: str, schema: str, payload: dict[str, Any], *, source: str = "external") -> bool:
    """Queue a message from another thread; it is published at the start of the next step.

    Returns False when the inbox is full and the message was dropped.
    """
    item = (topic, schema, dict(payload), source)
    with self._inbox_lock:
      if topic in self._latest_topics:
        if topic in self._inbox_latest:
          self._inbox_conflated[topic] = self._inbox_conflated.get(topic, 0) + 1
        self._inbox_latest[topic] = item
      elif self._inbox_msgs >= self._max_inbox:
        self._inbox_overflow[topic] = self._inbox_overflow.get(topic, 0) + 1
        return False
      else:
        self._inbox.append(("msg", item))
        self._inbox_msgs += 1
    self._wake.set()
    return True

  def request_estop(self, reason: str = "operator request", *, source: str = "api") -> None:
    """Stop actuators immediately (from the calling thread) and latch the e-stop."""
    self._hard_stop_all()
    with self._inbox_lock:
      self._inbox.append(("estop", (reason, source)))
    self._wake.set()

  def request_estop_clear(self, *, source: str = "api") -> None:
    with self._inbox_lock:
      self._inbox.append(("clear", source))
    self._wake.set()

  def call_node(self, name: str, fn: Callable[[LiveNode, NodeContext], Any], *, timeout_s: float = 2.0) -> Any:
    """Run `fn(node, ctx)` on the executor thread and return its result (from any thread).

    Exceptions raised by `fn` are re-raised here. Raises TimeoutError when the executor
    does not get to it within `timeout_s` (for example because the runtime has stopped).
    """
    if name not in self._slots:
      raise LiveRuntimeError(f"no node named {name!r}")
    done = threading.Event()
    box: dict[str, Any] = {}
    with self._inbox_lock:
      self._inbox.append(("call", (name, fn, box, done)))
    self._wake.set()
    if not done.wait(timeout_s):
      raise TimeoutError(f"node {name} did not answer within {timeout_s} s (is the runtime running?)")
    if "error" in box:
      raise box["error"]
    return box.get("result")

  def stop(self) -> None:
    """Ask `run()` to return after the current step.

    Lock-free, so it is safe to call from a signal handler; the loop notices within
    `max_idle_sleep_s`.
    """
    self._stop_requested = True

  @property
  def estop_engaged(self) -> bool:
    return self._estop

  @property
  def estop_reason(self) -> str:
    return self._estop_reason

  @property
  def fault_events(self) -> list[FaultEvent]:
    with self._fault_lock:
      return list(self._fault_events)

  # ------------------------------------------------------------------ lifecycle

  def start(self) -> None:
    if self._started:
      return
    now = self._clock.monotonic_ns()
    self._started_ns = now
    self._started_wall = time.time()
    ready: list[_Slot] = []
    for slot in self._slots.values():
      try:
        slot.node.setup(slot.ctx)
      except Exception as exc:
        self._record_fault("setup_failed", f"node {slot.name} setup failed: {exc!r}", node=slot.name, severity="critical")
        for done in reversed(ready):
          self._call_quietly(done, "teardown")
        raise LiveRuntimeError(f"node {slot.name} failed to set up: {exc}") from exc
      ready.append(slot)
      if slot.period_ns:
        slot.next_tick_ns = now
    self._started = True
    self._last_step_wall_ns = time.monotonic_ns()

  def shutdown(self) -> None:
    """Stop actuators and tear nodes down (reverse order). Safe to call more than once."""
    if self._stopped or not self._started:
      return
    self._stopped = True
    with self._state_lock:
      for slot in self._slots.values():
        self._call_quietly(slot, "safe_stop")
      self._hard_stop_all()
      for slot in reversed(list(self._slots.values())):
        self._call_quietly(slot, "teardown")

  def step(self) -> int:
    """Run one scheduling cycle and return the number of messages dispatched.

    Order: control inbox, pending messages, then each due tick in registration order with
    its outputs dispatched right away (so a producer registered before its consumer
    reaches it within the same step), then liveness checks.
    """
    if not self._started:
      self.start()
    with self._state_lock:
      now = self._clock.monotonic_ns()
      self._drain_inbox()
      self._hops_left = self._max_hops_per_step
      self._hop_limit_hit = False
      processed = self._dispatch()
      processed += self._run_due_ticks(now)
      self._check_liveness(now)
      self._steps += 1
      self._m_steps.inc()
      self._update_gauges(now)
      self._last_step_wall_ns = time.monotonic_ns()
      return processed

  def _next_tick_ns(self) -> int | None:
    if not self._tick_slots:
      return None
    return min(slot.next_tick_ns for slot in self._tick_slots)

  def next_wakeup_ns(self) -> int | None:
    if self._queue or self._inbox or self._inbox_latest:
      return self._clock.monotonic_ns()
    return self._next_tick_ns()

  def run(self, duration_s: float | None = None, stop_event: threading.Event | None = None) -> None:
    """Run until `duration_s` elapses (runtime clock), `stop_event` is set, or `stop()` is called."""
    if self._clock.simulated and duration_s is None and stop_event is None and self._tick_slots:
      raise LiveRuntimeError("a simulated clock needs duration_s (ticking nodes would otherwise run forever)")
    self.start()
    guard = None
    if not self._clock.simulated and self._stall_timeout_ns:
      guard = _StallGuard(self, self._stall_timeout_ns)
      guard.start()
    start = self._clock.monotonic_ns()
    deadline = start + int(duration_s * 1e9) if duration_s is not None else None
    try:
      while not self._stop_requested:
        if stop_event is not None and stop_event.is_set():
          break
        self.step()
        if self._stop_requested:
          break
        now = self._clock.monotonic_ns()
        if deadline is not None and now >= deadline:
          break
        if self._clock.simulated:
          wake = self._next_tick_ns()
          if self._queue or self._inbox or self._inbox_latest:
            # Only a hop-budget overflow leaves work queued; still advance so a message
            # loop cannot freeze simulated time.
            wake = now + 1_000_000 if wake is None else min(wake, now + 1_000_000)
          if wake is None:
            if deadline is None:
              break
            wake = deadline
          if deadline is not None:
            wake = min(wake, deadline)
          self._clock.sleep_until_ns(max(wake, now + 1))
        else:
          wake = self.next_wakeup_ns()
          target = now + self._max_idle_sleep_ns if wake is None else min(wake, now + self._max_idle_sleep_ns)
          if deadline is not None:
            target = min(target, deadline)
          if target > now:
            self._clock.sleep_until_ns(target, self._wake)
          self._wake.clear()
    finally:
      if guard is not None:
        guard.stop()
      self.shutdown()

  # ------------------------------------------------------------------ executor internals

  def _new_trace_id(self) -> str:
    return f"{self._rng.getrandbits(128):032x}"

  def _topic_stats(self, topic: str) -> _TopicStats:
    stats = self._topics.get(topic)
    if stats is None:
      stats = self._topics[topic] = _TopicStats()
      stats.latency = self.metrics.histogram("nervlynx_trace_latency_seconds", label_key({"topic": topic}))
    return stats

  def _emit(self, source: str, topic: str, schema: str, payload: dict[str, Any], trace_id: str | None) -> RuntimeMessage:
    now = self._clock.monotonic_ns()
    is_root = trace_id is None
    msg = self.publish(
      topic=topic,
      source=source,
      schema=schema,
      payload=payload,
      trace_id=self._new_trace_id() if is_root else trace_id,
    )
    tid = msg.envelope.trace_id
    stats = self._topic_stats(topic)
    stats.count += 1
    stats.recent.append(now)
    stats.last_payload = payload
    stats.last_source = source
    counter = stats.published_counters.get(source)
    if counter is None:
      counter = stats.published_counters[source] = self.metrics.counter(
        "nervlynx_messages_published_total", label_key({"source": source, "topic": topic})
      )
    counter.inc()
    self._published += 1
    if is_root:
      self._trace_origin[tid] = now
      if len(self._trace_origin) > _TRACE_ORIGIN_CAPACITY:
        del self._trace_origin[next(iter(self._trace_origin))]
    else:
      origin = self._trace_origin.get(tid)
      if origin is not None and stats.latency is not None:
        stats.latency.observe((now - origin) / 1e9)
    for listener in self._message_listeners:
      listener(msg)
    if len(self._queue) >= self._max_queue_size:
      stats.dropped += 1
      self._dropped += 1
      self.metrics.inc("nervlynx_messages_dropped_total", 1, label_key({"reason": "backpressure", "topic": topic}))
      self._record_fault(
        "backpressure",
        f"backpressure drop: queue full while inserting topic={topic}",
        dedupe_key=("backpressure", topic),
      )
      return msg
    self._enqueue_counter += 1
    if topic in self._latest_topics:
      self._latest_enqueued[topic] = self._enqueue_counter
    heapq.heappush(self._queue, (self._topic_priority.get(topic, 100), self._enqueue_counter, msg))
    if len(self._queue) > self._queue_depth_max:
      self._queue_depth_max = len(self._queue)
    return msg

  def _count_conflated(self, topic: str, n: int) -> None:
    self._topic_stats(topic).conflated += n
    self._conflated += n
    self.metrics.inc("nervlynx_messages_conflated_total", n, label_key({"topic": topic}))

  def _drain_inbox(self) -> None:
    with self._inbox_lock:
      items = list(self._inbox)
      self._inbox.clear()
      self._inbox_msgs = 0
      latest = list(self._inbox_latest.values())
      self._inbox_latest.clear()
      conflated, self._inbox_conflated = self._inbox_conflated, {}
      overflow, self._inbox_overflow = self._inbox_overflow, {}
    for topic, n in conflated.items():
      self._count_conflated(topic, n)
    for topic, n in overflow.items():
      self._topic_stats(topic).dropped += n
      self._dropped += n
      self.metrics.inc("nervlynx_messages_dropped_total", n, label_key({"reason": "inbox_full", "topic": topic}))
      self._record_fault("backpressure", f"inbox full: dropped {n} external message(s) on topic={topic}", dedupe_key=("inbox_full", topic))
    for kind, data in items:
      if kind == "msg":
        topic, schema, payload, source = data
        self._emit(source, topic, schema, payload, None)
      elif kind == "estop":
        reason, source = data
        self._engage_estop(reason, source=source)
      elif kind == "clear":
        self._clear_estop(source=data)
      elif kind == "call":
        name, fn, box, done = data
        slot = self._slots[name]
        try:
          box["result"] = fn(slot.node, slot.ctx)
        except Exception as exc:  # handed back to the caller
          box["error"] = exc
        finally:
          done.set()
    for topic, schema, payload, source in latest:
      self._emit(source, topic, schema, payload, None)

  def _run_due_ticks(self, now: int) -> int:
    processed = 0
    for slot in self._tick_slots:
      if now < slot.next_tick_ns:
        continue
      period = slot.period_ns or 1
      lateness = now - slot.next_tick_ns
      if lateness >= period:
        slot.overruns += 1
        slot.missed_ticks += lateness // period
        self.metrics.inc("nervlynx_tick_overruns_total", 1, label_key({"node": slot.name}))
        slot.next_tick_ns = now + period
      else:
        slot.next_tick_ns += period
      if slot.lateness_hist is not None:
        slot.lateness_hist.observe(lateness / 1e9)
      if self._breaker_blocks(slot, now):
        slot.skipped += 1
        continue
      slot.ctx._current_trace = None
      started = time.perf_counter_ns()
      try:
        out = slot.node.tick(slot.ctx)
      except Exception as exc:
        self._node_failed(slot, exc, "tick")
        continue
      finally:
        if slot.tick_hist is not None:
          slot.tick_hist.observe((time.perf_counter_ns() - started) / 1e9)
      slot.ticks += 1
      self._node_ok(slot)
      if out:
        for topic, schema, payload in out:
          self._emit(slot.name, topic, schema, payload, None)
      processed += self._dispatch()
    return processed

  def _dispatch(self) -> int:
    """Deliver queued messages until the queue empties or the step's hop budget runs out."""
    processed = 0
    while self._queue:
      if self._hops_left <= 0:
        if not self._hop_limit_hit:
          self._hop_limit_hit = True
          self._hop_limit_events += 1
          self._m_hops.inc()
          self._record_fault("hop_limit", "pipeline hop limit reached", dedupe_key="hop_limit")
        break
      _, counter, msg = heapq.heappop(self._queue)
      topic = msg.envelope.topic
      if topic in self._latest_topics and counter != self._latest_enqueued.get(topic):
        self._count_conflated(topic, 1)
        continue
      self._hops_left -= 1
      processed += 1
      for slot, handler in self._routes.get(msg.envelope.topic, ()):
        self._deliver(slot, handler, msg)
    return processed

  def _deliver(self, slot: _Slot, handler: Handler, msg: RuntimeMessage) -> None:
    if self._breaker_blocks(slot, self._clock.monotonic_ns()):
      slot.skipped += 1
      self.metrics.inc("nervlynx_messages_dropped_total", 1, label_key({"reason": "breaker_open", "topic": msg.envelope.topic}))
      return
    slot.ctx._current_trace = msg.envelope.trace_id
    started = time.perf_counter_ns()
    try:
      out = handler(msg)
    except Exception as exc:
      self._node_failed(slot, exc, f"on_message({msg.envelope.topic})")
      return
    finally:
      slot.ctx._current_trace = None
      if slot.handler_hist is not None:
        slot.handler_hist.observe((time.perf_counter_ns() - started) / 1e9)
    slot.handled += 1
    self._delivered += 1
    counter = slot.delivered_counters.get(msg.envelope.topic)
    if counter is not None:
      counter.inc()
    self._node_ok(slot)
    if out:
      for topic, schema, payload in out:
        self._emit(slot.name, topic, schema, payload, msg.envelope.trace_id)

  def _breaker_blocks(self, slot: _Slot, now: int) -> bool:
    return slot.breaker_open_until_ns != 0 and now < slot.breaker_open_until_ns

  def _node_ok(self, slot: _Slot) -> None:
    slot.last_ok_ns = self._clock.monotonic_ns()
    self._heartbeats[slot.name] = slot.last_ok_ns
    slot.consecutive_errors = 0
    if slot.breaker_open_until_ns:
      slot.breaker_open_until_ns = 0
      self.metrics.set_gauge("nervlynx_node_breaker_open", 0, label_key({"node": slot.name}))
      self._record_fault("breaker_closed", f"circuit breaker closed for node {slot.name}", node=slot.name, severity="info")

  def _node_failed(self, slot: _Slot, exc: Exception, where: str) -> None:
    now = self._clock.monotonic_ns()
    slot.errors += 1
    slot.consecutive_errors += 1
    slot.last_error = f"{type(exc).__name__}: {exc}"
    self.metrics.inc("nervlynx_node_errors_total", 1, label_key({"node": slot.name}))
    self._record_fault(
      "node_error",
      f"node {slot.name} {where} raised {slot.last_error}",
      node=slot.name,
      severity="error",
      dedupe_key=("node_error", slot.name),
    )
    if not self._breaker_threshold or slot.consecutive_errors < self._breaker_threshold:
      return
    if self._breaker_blocks(slot, now):
      return
    slot.breaker_open_until_ns = now + max(1, self._breaker_cooldown_ns)
    slot.breaker_trips += 1
    self.metrics.inc("nervlynx_node_breaker_trips_total", 1, label_key({"node": slot.name}))
    self.metrics.set_gauge("nervlynx_node_breaker_open", 1, label_key({"node": slot.name}))
    self._record_fault(
      "breaker_open",
      f"circuit breaker opened for node {slot.name} after {slot.consecutive_errors} consecutive errors",
      node=slot.name,
      severity="error",
    )
    self._call_quietly(slot, "safe_stop")
    if slot.critical:
      self._engage_estop(f"critical node {slot.name} tripped its circuit breaker", source="breaker")

  def _check_liveness(self, now: int) -> None:
    for slot in self._slots.values():
      if not slot.critical or slot.stale_after_ns is None:
        continue
      if slot.last_ok_ns is not None:
        ref = slot.last_ok_ns
      else:
        ref = self._started_ns if self._started_ns is not None else now
      age = now - ref
      if age > slot.stale_after_ns:
        if not slot.stale:
          slot.stale = True
          self.metrics.inc("nervlynx_watchdog_faults_total", 1, label_key({"node": slot.name}))
          self._record_fault("watchdog", f"node {slot.name} stale for {age / 1e9:.3f}s", node=slot.name, severity="critical")
          if self._estop_on_stale:
            self._engage_estop(f"watchdog: node {slot.name} stale", source="watchdog")
      elif slot.stale:
        slot.stale = False
        self._record_fault("watchdog_recovered", f"node {slot.name} is live again", node=slot.name, severity="info")

  def _engage_estop(self, reason: str, *, source: str) -> None:
    if self._estop:
      return
    self._estop = True
    self._estop_reason = reason
    self._estop_source = source
    self._estop_since_ns = self._clock.monotonic_ns()
    self._estop_events += 1
    self._m_estop.set(1)
    self.metrics.inc("nervlynx_estop_events_total", 1, label_key({"source": source}))
    self._record_fault("estop", f"e-stop engaged by {source}: {reason}", severity="critical")
    for slot in self._slots.values():
      self._call_quietly(slot, "safe_stop")
    self._emit("runtime", ESTOP_TOPIC, "EStopEvent", {"engaged": True, "reason": reason, "source": source}, None)

  def _clear_estop(self, *, source: str) -> bool:
    if not self._estop:
      return True
    stale = sorted(slot.name for slot in self._slots.values() if slot.stale)
    if stale:
      self._record_fault("estop_clear_refused", f"e-stop clear refused: stale critical nodes {','.join(stale)}", severity="warning")
      return False
    self._estop = False
    self._estop_reason = ""
    self._estop_source = ""
    self._estop_since_ns = None
    self._m_estop.set(0)
    self._record_fault("estop_cleared", f"e-stop cleared by {source}", severity="info")
    self._emit("runtime", ESTOP_TOPIC, "EStopEvent", {"engaged": False, "reason": "", "source": source}, None)
    return True

  def _hard_stop_all(self) -> None:
    failed: list[tuple[str, Exception]] = []
    for slot in list(self._slots.values()):
      try:
        slot.node.hard_stop()
      except Exception as exc:  # pragma: no cover - defensive, hardware specific
        failed.append((slot.name, exc))
    for name, exc in failed:
      self._record_fault("hard_stop_failed", f"node {name} hard_stop raised {exc!r}", node=name, severity="critical")

  def _on_stall(self, age_ns: int) -> None:
    # Stop first: recording the fault runs listeners, which may be slow.
    self.request_estop(f"executor stalled for {age_ns / 1e9:.3f}s", source="stall_guard")
    self._stalls += 1
    self.metrics.inc("nervlynx_executor_stalls_total")
    self._record_fault("stall", f"executor stalled for {age_ns / 1e9:.3f}s; actuators hard-stopped", severity="critical")

  def _call_quietly(self, slot: _Slot, method: str) -> None:
    try:
      getattr(slot.node, method)(slot.ctx)
    except Exception as exc:
      self._record_fault(f"{method}_failed", f"node {slot.name} {method} raised {exc!r}", node=slot.name, severity="critical")
      if method == "safe_stop":
        try:
          slot.node.hard_stop()
        except Exception:  # pragma: no cover - defensive
          pass

  def _record_fault(
    self,
    kind: str,
    message: str,
    *,
    node: str | None = None,
    severity: str = "warning",
    dedupe_key: Any = None,
  ) -> None:
    now = self._clock.monotonic_ns()
    self.metrics.inc("nervlynx_faults_total", 1, label_key({"kind": kind, "severity": severity}))
    with self._fault_lock:
      if dedupe_key is not None:
        last = self._fault_last_ns.get(dedupe_key)
        if last is not None and now - last < _FAULT_COOLDOWN_NS:
          return
        self._fault_last_ns[dedupe_key] = now
      event = FaultEvent(time_ns=now, kind=kind, severity=severity, message=message, node=node)
      self._fault_events.append(event)
      self._faults.append(message)
      listeners = list(self._fault_listeners)
    for listener in listeners:
      try:
        listener(event)
      except Exception:  # pragma: no cover - listeners must not break the executor
        pass

  def _update_gauges(self, now: int) -> None:
    self._m_queue.set(len(self._queue))
    if now - self._last_gauge_ns < 100_000_000:
      return
    self._last_gauge_ns = now
    if self._started_ns is not None:
      self._m_uptime.set((now - self._started_ns) / 1e9)
    self.metrics.set_gauge("nervlynx_queue_depth_max", self._queue_depth_max)
    for slot in self._slots.values():
      if slot.last_ok_ns is not None:
        self.metrics.set_gauge("nervlynx_node_heartbeat_age_seconds", (now - slot.last_ok_ns) / 1e9, label_key({"node": slot.name}))
    for topic, stats in self._topics.items():
      self.metrics.set_gauge("nervlynx_topic_rate_hz", round(stats.rate_hz(now), 3), label_key({"topic": topic}))

  def _describe_metrics(self) -> None:
    for name, text in (
      ("nervlynx_steps_total", "Executor scheduling cycles."),
      ("nervlynx_messages_published_total", "Messages published, by topic and source."),
      ("nervlynx_messages_delivered_total", "Messages delivered to a node, by topic and node."),
      ("nervlynx_messages_dropped_total", "Messages not delivered, by topic and reason."),
      ("nervlynx_messages_conflated_total", "Messages superseded by a newer one on a latest-value topic."),
      ("nervlynx_handler_seconds", "Time spent in node on_message handlers."),
      ("nervlynx_tick_seconds", "Time spent in node tick callbacks."),
      ("nervlynx_tick_lateness_seconds", "How late each tick started relative to its schedule."),
      ("nervlynx_tick_overruns_total", "Ticks that started a full period or more late."),
      ("nervlynx_trace_latency_seconds", "Time from a trace's root message to each downstream message."),
      ("nervlynx_node_errors_total", "Exceptions raised by node callbacks."),
      ("nervlynx_node_breaker_trips_total", "Circuit breaker openings per node."),
      ("nervlynx_node_breaker_open", "1 while a node's circuit breaker is open."),
      ("nervlynx_watchdog_faults_total", "Critical nodes that went stale."),
      ("nervlynx_estop_engaged", "1 while the e-stop is latched."),
      ("nervlynx_estop_events_total", "E-stop engagements, by source."),
      ("nervlynx_executor_stalls_total", "Times the stall guard hard-stopped actuators."),
      ("nervlynx_faults_total", "Recorded faults, by kind and severity."),
      ("nervlynx_queue_depth", "Messages waiting in the dispatch queue."),
      ("nervlynx_uptime_seconds", "Time since the runtime started."),
      ("nervlynx_topic_rate_hz", "Recent publish rate per topic."),
    ):
      self.metrics.describe(name, text)

  # ------------------------------------------------------------------ introspection

  def last_payload(self, topic: str) -> dict[str, Any] | None:
    """A copy of the newest payload published on `topic` (safe from any thread)."""
    with self._state_lock:
      stats = self._topics.get(topic)
      if stats is None or stats.last_payload is None:
        return None
      return json.loads(json.dumps(stats.last_payload, default=str))

  def health(self) -> dict[str, Any]:
    with self._state_lock:
      return self._health_locked()

  def _health_locked(self) -> dict[str, Any]:
    stale = sorted(slot.name for slot in self._slots.values() if slot.stale)
    now = self._clock.monotonic_ns()
    open_breakers = sorted(slot.name for slot in self._slots.values() if self._breaker_blocks(slot, now))
    if self._estop:
      status = "estop"
    elif stale:
      status = "fault"
    elif open_breakers:
      status = "degraded"
    else:
      status = "ok"
    return {
      "status": status,
      "estop": self._estop,
      "stale_nodes": stale,
      "open_breakers": open_breakers,
      "uptime_s": (now - self._started_ns) / 1e9 if self._started_ns is not None else 0.0,
    }

  def snapshot(self, *, fault_limit: int = 25) -> dict[str, Any]:
    """Consistent, JSON-serialisable view of the whole runtime (safe from any thread)."""
    with self._state_lock:
      now = self._clock.monotonic_ns()
      nodes: dict[str, Any] = {}
      for name, slot in self._slots.items():
        entry: dict[str, Any] = {
          "input_topics": list(slot.topics),
          "rate_hz": round(1e9 / slot.period_ns, 3) if slot.period_ns else None,
          "critical": slot.critical,
          "ticks": slot.ticks,
          "handled": slot.handled,
          "errors": slot.errors,
          "last_error": slot.last_error,
          "skipped": slot.skipped,
          "overruns": slot.overruns,
          "missed_ticks": slot.missed_ticks,
          "breaker_open": self._breaker_blocks(slot, now),
          "breaker_trips": slot.breaker_trips,
          "stale": slot.stale,
          "heartbeat_age_s": round((now - slot.last_ok_ns) / 1e9, 4) if slot.last_ok_ns is not None else None,
          "handler_ms": _summary_ms(slot.handler_hist),
        }
        if slot.period_ns:
          entry["tick_ms"] = _summary_ms(slot.tick_hist)
          entry["lateness_ms"] = _summary_ms(slot.lateness_hist)
        try:
          extra = slot.node.status()
        except Exception as exc:  # pragma: no cover - status must never break introspection
          extra = {"status_error": repr(exc)}
        if extra:
          entry["status"] = extra
        nodes[name] = entry
      topics = {
        topic: {
          "count": stats.count,
          "rate_hz": round(stats.rate_hz(now), 3),
          "dropped": stats.dropped,
          "conflated": stats.conflated,
          "latest_only": topic in self._latest_topics,
          "subscribers": [slot.name for slot, _ in self._routes.get(topic, ())],
          "last_source": stats.last_source,
          "latency_ms": _summary_ms(stats.latency),
          "last": _payload_preview(stats.last_payload),
        }
        for topic, stats in sorted(self._topics.items())
      }
      with self._fault_lock:
        recent = list(self._fault_events)[-fault_limit:] if fault_limit > 0 else []
      started = self._started_ns
      faults = [
        {**event.to_dict(), "t_s": round((event.time_ns - started) / 1e9, 3) if started is not None else None}
        for event in recent
      ]
      return {
        "name": self.name,
        "clock": "simulated" if self._clock.simulated else "system",
        "started_at_unix": self._started_wall,
        "health": self._health_locked(),
        "estop": {
          "engaged": self._estop,
          "reason": self._estop_reason,
          "source": self._estop_source,
          "since_s": round((now - self._estop_since_ns) / 1e9, 3) if self._estop_since_ns is not None else None,
          "events": self._estop_events,
        },
        "executor": {
          "steps": self._steps,
          "queue_depth": len(self._queue),
          "queue_depth_max": self._queue_depth_max,
          "hop_limit_events": self._hop_limit_events,
          "stalls": self._stalls,
        },
        "messages": {"published": self._published, "delivered": self._delivered, "dropped": self._dropped, "conflated": self._conflated},
        "nodes": nodes,
        "topics": topics,
        "faults": faults,
      }
