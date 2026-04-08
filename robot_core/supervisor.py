from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class ManagedNode:
  name: str
  start: Callable[[], None]
  stop: Callable[[], None]
  dependencies: tuple[str, ...] = field(default_factory=tuple)


class RuntimeSupervisor:
  """Lifecycle orchestration with dependency-aware startup order."""

  def __init__(self) -> None:
    self._nodes: dict[str, ManagedNode] = {}
    self._started_order: list[str] = []

  def register(self, node: ManagedNode) -> None:
    self._nodes[node.name] = node

  def _topological_order(self) -> list[str]:
    pending = {name: set(node.dependencies) for name, node in self._nodes.items()}
    order: list[str] = []
    while pending:
      ready = sorted([n for n, deps in pending.items() if not deps])
      if not ready:
        raise RuntimeError("cycle detected in node dependencies")
      for n in ready:
        order.append(n)
        del pending[n]
        for deps in pending.values():
          deps.discard(n)
    return order

  def start_all(self) -> None:
    for name in self._topological_order():
      self._nodes[name].start()
      self._started_order.append(name)

  def stop_all(self) -> None:
    for name in reversed(self._started_order):
      self._nodes[name].stop()
    self._started_order.clear()

  @property
  def started_order(self) -> list[str]:
    return list(self._started_order)
