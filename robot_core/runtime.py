from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
import heapq
from typing import Any, Awaitable, Callable
import time
import uuid


@dataclass(frozen=True)
class Envelope:
  topic: str
  source: str
  sequence: int
  monotonic_time_ns: int
  trace_id: str
  schema: str


@dataclass(frozen=True)
class RuntimeMessage:
  envelope: Envelope
  payload: dict[str, Any]


NodeHandler = Callable[[RuntimeMessage], list[tuple[str, str, dict[str, Any]]] | None]
AsyncNodeHandler = Callable[[RuntimeMessage], Awaitable[list[tuple[str, str, dict[str, Any]]] | None]]


class Clock:
  def monotonic_ns(self) -> int:
    raise NotImplementedError


class SystemClock(Clock):
  def monotonic_ns(self) -> int:
    return time.monotonic_ns()


class SimulatedClock(Clock):
  def __init__(self, start_ns: int = 0) -> None:
    self._now_ns = start_ns

  def monotonic_ns(self) -> int:
    return self._now_ns

  def advance_ms(self, dt_ms: float) -> None:
    self._now_ns += int(dt_ms * 1e6)


class PipelineRuntime:
  """In-process runtime with priority and queue backpressure support."""

  def __init__(
    self,
    *,
    max_queue_size: int = 2048,
    topic_priority: dict[str, int] | None = None,
    clock: Clock | None = None,
  ) -> None:
    self._subscriptions: dict[str, list[tuple[str, NodeHandler]]] = defaultdict(list)
    self._sequences: dict[tuple[str, str], int] = defaultdict(int)
    self._heartbeats: dict[str, int] = {}
    self._faults: list[str] = []
    self._queue: list[tuple[int, int, RuntimeMessage]] = []
    self._enqueue_counter = 0
    self._max_queue_size = max_queue_size
    self._topic_priority = topic_priority or {}
    self._clock = clock or SystemClock()

  def subscribe(self, node_name: str, topic: str, handler: NodeHandler) -> None:
    self._subscriptions[topic].append((node_name, handler))

  def publish(
    self,
    *,
    topic: str,
    source: str,
    schema: str,
    payload: dict[str, Any],
    trace_id: str | None = None,
  ) -> RuntimeMessage:
    key = (source, topic)
    seq = self._sequences[key] + 1
    self._sequences[key] = seq
    env = Envelope(
      topic=topic,
      source=source,
      sequence=seq,
      monotonic_time_ns=self._clock.monotonic_ns(),
      trace_id=trace_id or uuid.uuid4().hex,
      schema=schema,
    )
    return RuntimeMessage(envelope=env, payload=payload)

  def _push(self, msg: RuntimeMessage) -> bool:
    if len(self._queue) >= self._max_queue_size:
      self._faults.append(f"backpressure drop: queue full while inserting topic={msg.envelope.topic}")
      return False
    pri = self._topic_priority.get(msg.envelope.topic, 100)
    self._enqueue_counter += 1
    heapq.heappush(self._queue, (pri, self._enqueue_counter, msg))
    return True

  def run_once(self, root_message: RuntimeMessage, max_hops: int = 1024) -> list[RuntimeMessage]:
    """Push one seed message through graph and return full trace."""
    seen: list[RuntimeMessage] = []
    self._queue.clear()
    self._push(root_message)
    hops = 0
    while self._queue:
      hops += 1
      if hops > max_hops:
        self._faults.append("pipeline hop limit reached")
        break
      _, _, current = heapq.heappop(self._queue)
      seen.append(current)
      for node_name, handler in self._subscriptions.get(current.envelope.topic, []):
        out = handler(current) or []
        self._heartbeats[node_name] = self._clock.monotonic_ns()
        for out_topic, out_schema, out_payload in out:
          self._push(
            self.publish(
              topic=out_topic,
              source=node_name,
              schema=out_schema,
              payload=out_payload,
              trace_id=current.envelope.trace_id,
            )
          )
    return seen

  def run_stream(self, roots: list[RuntimeMessage], max_hops: int = 4096) -> list[RuntimeMessage]:
    """Process a batch of seeds in one deterministic scheduling pass."""
    trace: list[RuntimeMessage] = []
    for root in roots:
      trace.extend(self.run_once(root, max_hops=max_hops))
    return trace

  @property
  def node_heartbeats_ns(self) -> dict[str, int]:
    return dict(self._heartbeats)

  @property
  def faults(self) -> list[str]:
    return list(self._faults)


class AsyncPipelineRuntime(PipelineRuntime):
  """Asynchronous runtime for continuous event-loop based processing."""

  def __init__(
    self,
    *,
    max_queue_size: int = 2048,
    topic_priority: dict[str, int] | None = None,
    clock: Clock | None = None,
  ) -> None:
    super().__init__(max_queue_size=max_queue_size, topic_priority=topic_priority, clock=clock)
    self._async_subscriptions: dict[str, list[tuple[str, AsyncNodeHandler]]] = defaultdict(list)

  def subscribe_async(self, node_name: str, topic: str, handler: AsyncNodeHandler) -> None:
    self._async_subscriptions[topic].append((node_name, handler))

  async def run_once_async(self, root_message: RuntimeMessage, max_hops: int = 1024) -> list[RuntimeMessage]:
    seen: list[RuntimeMessage] = []
    pq: asyncio.PriorityQueue[tuple[int, int, RuntimeMessage]] = asyncio.PriorityQueue(maxsize=self._max_queue_size)
    counter = 0
    await pq.put((self._topic_priority.get(root_message.envelope.topic, 100), counter, root_message))
    hops = 0
    while not pq.empty():
      hops += 1
      if hops > max_hops:
        self._faults.append("pipeline hop limit reached")
        break
      _, _, current = await pq.get()
      seen.append(current)
      for node_name, handler in self._async_subscriptions.get(current.envelope.topic, []):
        out = await handler(current) or []
        self._heartbeats[node_name] = self._clock.monotonic_ns()
        for out_topic, out_schema, out_payload in out:
          counter += 1
          msg = self.publish(
            topic=out_topic,
            source=node_name,
            schema=out_schema,
            payload=out_payload,
            trace_id=current.envelope.trace_id,
          )
          if pq.full():
            self._faults.append(f"backpressure drop: queue full while inserting topic={msg.envelope.topic}")
            continue
          await pq.put((self._topic_priority.get(out_topic, 100), counter, msg))
    return seen
