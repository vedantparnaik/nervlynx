from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from robot_core.plugins import PluginRegistry
from robot_core.runtime import PipelineRuntime


def load_graph_config(path: str | Path) -> dict[str, Any]:
  p = Path(path)
  with p.open("r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
  return dict(raw)


def wire_graph_from_config(runtime: PipelineRuntime, registry: PluginRegistry, config: dict[str, Any]) -> None:
  nodes = config.get("nodes", [])
  for node_cfg in nodes:
    plugin_name = str(node_cfg["plugin"])
    node_name = str(node_cfg.get("name", plugin_name))
    node = registry.get_node(plugin_name)
    topics = node_cfg.get("input_topics", node.input_topics)
    for topic in topics:
      runtime.subscribe(node_name=node_name, topic=str(topic), handler=node.handle)
