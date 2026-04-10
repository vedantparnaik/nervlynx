from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from robot_core.plugins import PluginRegistry
from robot_core.runtime import Envelope, RuntimeMessage
from robot_core.security import (
  TopicAccessPolicy,
  check_publish_allowed,
  check_subscribe_allowed,
  sign_payload,
  verify_payload_signature,
)
from robot_core.transport import Transport


@dataclass(frozen=True)
class DistributedNodeConfig:
  node_name: str
  plugin_name: str
  subscribe_topics: tuple[str, ...]
  policy: TopicAccessPolicy
  secret: str


class DistributedNodeRunner:
  """Runs one node over a transport boundary."""

  def __init__(self, transport: Transport, registry: PluginRegistry, config: DistributedNodeConfig) -> None:
    self._transport = transport
    self._registry = registry
    self._config = config
    self._plugin = registry.get_node(config.plugin_name)
    self._seq = 0

  def start(self) -> None:
    for topic in self._config.subscribe_topics:
      if not check_subscribe_allowed(topic, self._config.policy):
        continue
      self._transport.subscribe(topic, self._on_message)

  def _on_message(self, msg: RuntimeMessage) -> None:
    signature = str(msg.payload.get("_signature", ""))
    unsigned = {k: v for k, v in msg.payload.items() if k != "_signature"}
    if signature and not verify_payload_signature(unsigned, signature, self._config.secret):
      return
    out = self._plugin.handle(RuntimeMessage(envelope=msg.envelope, payload=unsigned))
    for out_topic, out_schema, out_payload in out:
      if not check_publish_allowed(out_topic, self._config.policy):
        continue
      self._seq += 1
      signed_payload: dict[str, Any] = dict(out_payload)
      signed_payload["_signature"] = sign_payload(out_payload, self._config.secret)
      out_msg = RuntimeMessage(
        envelope=Envelope(
          topic=out_topic,
          source=self._config.node_name,
          sequence=self._seq,
          monotonic_time_ns=msg.envelope.monotonic_time_ns,
          trace_id=msg.envelope.trace_id,
          schema=out_schema,
        ),
        payload=signed_payload,
      )
      self._transport.publish(out_msg)
