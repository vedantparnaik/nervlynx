from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable
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


class PipelineRuntime:
  """In-process pipeline runtime for deterministic robotics dataflow tests."""

  def __init__(self) -> None:
    self._subscriptions: dict[str, list[tuple[str, NodeHandler]]] = defaultdict(list)
    self._sequences: dict[tuple[str, str], int] = defaultdict(int)
    self._heartbeats: dict[str, int] = {}
    self._faults: list[str] = []

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
      monotonic_time_ns=time.monotonic_ns(),
      trace_id=trace_id or uuid.uuid4().hex,
      schema=schema,
    )
    return RuntimeMessage(envelope=env, payload=payload)

  def run_once(self, root_message: RuntimeMessage, max_hops: int = 256) -> list[RuntimeMessage]:
    """Pushes one message through the graph and returns full trace."""
    seen: list[RuntimeMessage] = []
    queue: deque[RuntimeMessage] = deque([root_message])
    hops = 0
    while queue:
      hops += 1
      if hops > max_hops:
        self._faults.append("pipeline hop limit reached")
        break
      current = queue.popleft()
      seen.append(current)
      for node_name, handler in self._subscriptions.get(current.envelope.topic, []):
        out = handler(current) or []
        self._heartbeats[node_name] = time.monotonic_ns()
        for out_topic, out_schema, out_payload in out:
          queue.append(
            self.publish(
              topic=out_topic,
              source=node_name,
              schema=out_schema,
              payload=out_payload,
              trace_id=current.envelope.trace_id,
            )
          )
    return seen

  @property
  def node_heartbeats_ns(self) -> dict[str, int]:
    return dict(self._heartbeats)

  @property
  def faults(self) -> list[str]:
    return list(self._faults)
