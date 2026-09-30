"""NervLynx: write robot nodes in plain Python and run them in simulation or on the robot.

    from nervlynx import node

    @node(inputs=["range.front"], outputs="cmd.drive", rate_hz=20)
    def avoid(front, *, stop_m=0.3):
      ...

`LiveNode` is the class-based API for drivers that own hardware; `NodeContext` is what
their hooks receive.
"""

from typing import Any

from robot_core.live import LiveNode, NodeContext
from robot_core.node_api import node

__all__ = ["node", "LiveNode", "NodeContext", "__version__"]


def __getattr__(name: str) -> Any:
  if name != "__version__":
    raise AttributeError(f"module 'nervlynx' has no attribute {name!r}")
  from importlib.metadata import PackageNotFoundError, version

  try:
    value = version("nervlynx")
  except PackageNotFoundError:  # pragma: no cover - running from a source checkout without install
    value = "0+unknown"
  globals()["__version__"] = value
  return value
