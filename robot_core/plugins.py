from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from importlib import import_module
from importlib.metadata import entry_points
from typing import Any, Callable, Protocol

from robot_core.runtime import RuntimeMessage


class LazyFactory:
  """A live-node factory named "module:attr", imported the first time it is used.

  Built-in drivers register this way so a robot only imports the drivers its config
  names, which keeps start-up fast on small boards.
  """

  def __init__(self, path: str) -> None:
    module, _, attr = path.partition(":")
    if not module or not attr:
      raise ValueError(f"lazy factory path must look like 'package.module:Name', got {path!r}")
    self.path = path
    self._target: Callable[..., Any] | None = None

  def resolve(self) -> Callable[..., Any]:
    if self._target is None:
      module, _, attr = self.path.partition(":")
      self._target = getattr(import_module(module), attr)
    return self._target

  @property
  def __signature__(self) -> inspect.Signature:
    return inspect.signature(self.resolve())

  def __call__(self, *args: Any, **kwargs: Any) -> Any:
    return self.resolve()(*args, **kwargs)

  def __repr__(self) -> str:
    return f"LazyFactory({self.path!r})"


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
  live_nodes: list[str] = field(default_factory=list)


class PluginRegistry:
  def __init__(self) -> None:
    self._sensors: dict[str, SensorPlugin] = {}
    self._nodes: dict[str, NodePlugin] = {}
    self._live_nodes: dict[str, Callable[..., Any]] = {}

  def register_sensor(self, plugin: SensorPlugin) -> None:
    self._sensors[plugin.name] = plugin

  def register_node(self, plugin: NodePlugin) -> None:
    self._nodes[plugin.name] = plugin

  def register_live_node(self, name: str, factory: Callable[..., Any]) -> None:
    """Register a `LiveNode` factory; graph `params` are passed to it as keyword arguments."""
    self._live_nodes[name] = factory

  def get_sensor(self, name: str) -> SensorPlugin:
    if name not in self._sensors:
      raise KeyError(f"sensor plugin not found: {name}")
    return self._sensors[name]

  def get_node(self, name: str) -> NodePlugin:
    if name not in self._nodes:
      raise KeyError(f"node plugin not found: {name}")
    return self._nodes[name]

  def get_live_node_factory(self, name: str) -> Callable[..., Any]:
    if name not in self._live_nodes:
      raise KeyError(f"live node plugin not found: {name}")
    return self._live_nodes[name]

  def has_sensor(self, name: str) -> bool:
    return name in self._sensors

  def has_node(self, name: str) -> bool:
    return name in self._nodes

  def has_live_node(self, name: str) -> bool:
    return name in self._live_nodes

  def catalog(self) -> PluginCatalog:
    return PluginCatalog(sensors=sorted(self._sensors), nodes=sorted(self._nodes), live_nodes=sorted(self._live_nodes))

  def discover_entrypoints(self) -> None:
    """Load plugins from Python entry points.

    - group `nervlynx.sensors`: callable returning SensorPlugin instance
    - group `nervlynx.nodes`: callable returning NodePlugin instance
    - group `nervlynx.live_nodes`: LiveNode factory, called later with graph `params`
    """
    sensor_eps = entry_points(group="nervlynx.sensors")
    node_eps = entry_points(group="nervlynx.nodes")
    for ep in sensor_eps:
      plugin_factory = ep.load()
      self.register_sensor(plugin_factory())
    for ep in node_eps:
      plugin_factory = ep.load()
      self.register_node(plugin_factory())
    for ep in entry_points(group="nervlynx.live_nodes"):
      self.register_live_node(ep.name, ep.load())
