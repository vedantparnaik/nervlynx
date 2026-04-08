from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import zmq

from shuttle.timeutil import monotonic_ns


@dataclass(frozen=True)
class Envelope:
  topic: str
  source: str
  sequence: int
  monotonic_time_ns: int
  schema: str


def _encode_header(env: Envelope) -> bytes:
  return json.dumps(
    {
      "topic": env.topic,
      "source": env.source,
      "sequence": env.sequence,
      "monotonic_time_ns": env.monotonic_time_ns,
      "schema": env.schema,
    },
    separators=(",", ":"),
  ).encode("utf-8")


def _decode_header(raw: bytes) -> Envelope:
  o = json.loads(raw.decode("utf-8"))
  return Envelope(
    topic=str(o["topic"]),
    source=str(o["source"]),
    sequence=int(o["sequence"]),
    monotonic_time_ns=int(o["monotonic_time_ns"]),
    schema=str(o["schema"]),
  )


class Publisher:
  def __init__(self, publisher_address: str, source: str) -> None:
    self._ctx = zmq.Context.instance()
    self._socket = self._ctx.socket(zmq.PUB)
    self._socket.connect(publisher_address)
    self._source = source
    self._seq: dict[str, int] = {}

  def send(self, topic: str, schema: str, payload: bytes) -> Envelope:
    seq = self._seq.get(topic, 0) + 1
    self._seq[topic] = seq
    env = Envelope(
      topic=topic,
      source=self._source,
      sequence=seq,
      monotonic_time_ns=monotonic_ns(),
      schema=schema,
    )
    self._socket.send_multipart([topic.encode("utf-8"), _encode_header(env), payload])
    return env

  def close(self) -> None:
    self._socket.close(0)


class Subscriber:
  def __init__(self, subscriber_address: str, topics: list[str]) -> None:
    self._ctx = zmq.Context.instance()
    self._socket = self._ctx.socket(zmq.SUB)
    self._socket.connect(subscriber_address)
    for t in topics:
      self._socket.setsockopt(zmq.SUBSCRIBE, t.encode("utf-8"))

  def recv(self, timeout_ms: int = 1000) -> tuple[Envelope, bytes] | None:
    if not self._socket.poll(timeout_ms, zmq.POLLIN):
      return None
    topic_raw, header_raw, payload = self._socket.recv_multipart()
    env = _decode_header(header_raw)
    topic = topic_raw.decode("utf-8")
    if topic != env.topic:
      raise RuntimeError(f"topic mismatch: {topic} != {env.topic}")
    return env, payload

  def close(self) -> None:
    self._socket.close(0)


def capnp_to_bytes(msg: Any) -> bytes:
  return msg.to_bytes()
