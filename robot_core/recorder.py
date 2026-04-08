from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from robot_core.runtime import Envelope, RuntimeMessage


def _encode(msg: RuntimeMessage) -> dict[str, object]:
  e = msg.envelope
  return {
    "topic": e.topic,
    "source": e.source,
    "sequence": e.sequence,
    "monotonic_time_ns": e.monotonic_time_ns,
    "trace_id": e.trace_id,
    "schema": e.schema,
    "payload": msg.payload,
  }


def _decode(row: dict[str, object]) -> RuntimeMessage:
  env = Envelope(
    topic=str(row["topic"]),
    source=str(row["source"]),
    sequence=int(row["sequence"]),
    monotonic_time_ns=int(row["monotonic_time_ns"]),
    trace_id=str(row["trace_id"]),
    schema=str(row["schema"]),
  )
  payload = dict(row["payload"])  # type: ignore[arg-type]
  return RuntimeMessage(envelope=env, payload=payload)


def write_jsonl(path: str | Path, messages: Iterable[RuntimeMessage]) -> None:
  p = Path(path)
  p.parent.mkdir(parents=True, exist_ok=True)
  with p.open("w", encoding="utf-8") as f:
    for msg in messages:
      f.write(json.dumps(_encode(msg), separators=(",", ":")) + "\n")


def read_jsonl(path: str | Path) -> list[RuntimeMessage]:
  p = Path(path)
  out: list[RuntimeMessage] = []
  with p.open("r", encoding="utf-8") as f:
    for line in f:
      if not line.strip():
        continue
      out.append(_decode(json.loads(line)))
  return out
