from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from robot_core.runtime import RuntimeMessage


class SensorPlugin(Protocol):
  name: str

  def read(self) -> dict[str, object]:
    """Read one normalized sensor bundle sample."""


class NodePlugin(Protocol):
  name: str
  input_topics: list[str]

  def handle(self, msg: RuntimeMessage) -> list[tuple[str, str, dict[str, object]]]:
    """Process one message and optionally emit outputs."""


@dataclass(frozen=True)
class PluginCatalog:
  sensors: list[str]
  nodes: list[str]


class PluginRegistry:
  def __init__(self) -> None:
    self._sensors: dict[str, SensorPlugin] = {}
    self._nodes: dict[str, NodePlugin] = {}

  def register_sensor(self, plugin: SensorPlugin) -> None:
    self._sensors[plugin.name] = plugin

  def register_node(self, plugin: NodePlugin) -> None:
    self._nodes[plugin.name] = plugin

  def get_sensor(self, name: str) -> SensorPlugin:
    if name not in self._sensors:
      raise KeyError(f"sensor plugin not found: {name}")
    return self._sensors[name]

  def get_node(self, name: str) -> NodePlugin:
    if name not in self._nodes:
      raise KeyError(f"node plugin not found: {name}")
    return self._nodes[name]

  def catalog(self) -> PluginCatalog:
    return PluginCatalog(sensors=sorted(self._sensors), nodes=sorted(self._nodes))
