from dataclasses import dataclass

from robot_core.plugins import PluginRegistry
from robot_core.runtime import Envelope, RuntimeMessage


@dataclass(frozen=True)
class DummySensor:
  name: str = "dummy_sensor"

  def read(self) -> dict[str, object]:
    return {"ok": True}


@dataclass(frozen=True)
class DummyNode:
  name: str = "dummy_node"
  input_topics: list[str] = None  # type: ignore[assignment]

  def __post_init__(self) -> None:
    object.__setattr__(self, "input_topics", ["in.topic"])

  def handle(self, msg: RuntimeMessage) -> list[tuple[str, str, dict[str, object]]]:
    return [("out.topic", "OutSchema", {"seen_topic": msg.envelope.topic})]


def test_plugin_registry_registers_and_catalogs() -> None:
  reg = PluginRegistry()
  reg.register_sensor(DummySensor())
  reg.register_node(DummyNode())
  cat = reg.catalog()
  assert cat.sensors == ["dummy_sensor"]
  assert cat.nodes == ["dummy_node"]


def test_plugin_node_can_process_message() -> None:
  node = DummyNode()
  msg = RuntimeMessage(
    envelope=Envelope(
      topic="in.topic",
      source="src",
      sequence=1,
      monotonic_time_ns=1,
      trace_id="t",
      schema="InSchema",
    ),
    payload={"x": 1},
  )
  out = node.handle(msg)
  assert out[0][0] == "out.topic"
