from __future__ import annotations

import json
from typing import Callable

from robot_core.runtime import Envelope, RuntimeMessage


TransportCallback = Callable[[RuntimeMessage], None]


class Transport:
  def publish(self, msg: RuntimeMessage) -> None:
    raise NotImplementedError

  def subscribe(self, topic: str, callback: TransportCallback) -> None:
    raise NotImplementedError

  def close(self) -> None:
    raise NotImplementedError


class InMemoryTransport(Transport):
  def __init__(self) -> None:
    self._subs: dict[str, list[TransportCallback]] = {}

  def publish(self, msg: RuntimeMessage) -> None:
    for cb in self._subs.get(msg.envelope.topic, []):
      cb(msg)

  def subscribe(self, topic: str, callback: TransportCallback) -> None:
    self._subs.setdefault(topic, []).append(callback)

  def close(self) -> None:
    self._subs.clear()


class ZmqJsonTransport(Transport):
  """Simple JSON transport for multi-process local deployment."""

  def __init__(self, pub_address: str, sub_address: str) -> None:
    import zmq

    self._ctx = zmq.Context.instance()
    self._pub = self._ctx.socket(zmq.PUB)
    self._pub.connect(pub_address)
    self._sub = self._ctx.socket(zmq.SUB)
    self._sub.connect(sub_address)
    self._callbacks: dict[str, list[TransportCallback]] = {}

  def publish(self, msg: RuntimeMessage) -> None:
    row = {
      "topic": msg.envelope.topic,
      "source": msg.envelope.source,
      "sequence": msg.envelope.sequence,
      "monotonic_time_ns": msg.envelope.monotonic_time_ns,
      "trace_id": msg.envelope.trace_id,
      "schema": msg.envelope.schema,
      "payload": msg.payload,
    }
    self._pub.send_multipart([msg.envelope.topic.encode("utf-8"), json.dumps(row).encode("utf-8")])

  def subscribe(self, topic: str, callback: TransportCallback) -> None:
    import zmq

    self._callbacks.setdefault(topic, []).append(callback)
    self._sub.setsockopt(zmq.SUBSCRIBE, topic.encode("utf-8"))

  def poll_once(self, timeout_ms: int = 50) -> bool:
    import zmq

    if not self._sub.poll(timeout_ms, zmq.POLLIN):
      return False
    raw_topic, raw = self._sub.recv_multipart()
    topic = raw_topic.decode("utf-8")
    row = json.loads(raw.decode("utf-8"))
    msg = RuntimeMessage(
      envelope=Envelope(
        topic=topic,
        source=str(row["source"]),
        sequence=int(row["sequence"]),
        monotonic_time_ns=int(row["monotonic_time_ns"]),
        trace_id=str(row["trace_id"]),
        schema=str(row["schema"]),
      ),
      payload=dict(row["payload"]),
    )
    for cb in self._callbacks.get(topic, []):
      cb(msg)
    return True

  def close(self) -> None:
    self._pub.close(0)
    self._sub.close(0)
