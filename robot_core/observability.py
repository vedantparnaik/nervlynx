from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from robot_core.runtime import RuntimeMessage


@dataclass(frozen=True)
class TopicStats:
  topic: str
  count: int
  avg_delta_ms: float


@dataclass(frozen=True)
class FlowStats:
  trace_id: str
  topic_count: int
  end_to_end_ms: float


def timeline_by_trace(messages: list[RuntimeMessage]) -> dict[str, list[RuntimeMessage]]:
  grouped: dict[str, list[RuntimeMessage]] = {}
  for msg in messages:
    grouped.setdefault(msg.envelope.trace_id, []).append(msg)
  for trace_msgs in grouped.values():
    trace_msgs.sort(key=lambda m: m.envelope.monotonic_time_ns)
  return grouped


def topic_latency_stats(messages: list[RuntimeMessage]) -> list[TopicStats]:
  by_topic: dict[str, list[int]] = {}
  for msg in messages:
    by_topic.setdefault(msg.envelope.topic, []).append(msg.envelope.monotonic_time_ns)
  stats: list[TopicStats] = []
  for topic, stamps in sorted(by_topic.items()):
    stamps.sort()
    if len(stamps) < 2:
      stats.append(TopicStats(topic=topic, count=len(stamps), avg_delta_ms=0.0))
      continue
    deltas = [stamps[i] - stamps[i - 1] for i in range(1, len(stamps))]
    avg_ms = (sum(deltas) / len(deltas)) / 1e6
    stats.append(TopicStats(topic=topic, count=len(stamps), avg_delta_ms=avg_ms))
  return stats


def structured_event(kind: str, detail: dict[str, Any]) -> dict[str, Any]:
  return {"event_kind": kind, "detail": detail}


def flow_stats(messages: list[RuntimeMessage]) -> list[FlowStats]:
  grouped = timeline_by_trace(messages)
  out: list[FlowStats] = []
  for trace_id, trace in grouped.items():
    if not trace:
      continue
    start_ns = trace[0].envelope.monotonic_time_ns
    end_ns = trace[-1].envelope.monotonic_time_ns
    out.append(
      FlowStats(
        trace_id=trace_id,
        topic_count=len(trace),
        end_to_end_ms=max(0.0, (end_ns - start_ns) / 1e6),
      )
    )
  out.sort(key=lambda x: x.end_to_end_ms, reverse=True)
  return out
