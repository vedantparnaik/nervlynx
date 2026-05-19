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


def _is_non_empty_string(value: object) -> bool:
  return isinstance(value, str) and bool(value.strip())


def validate_graph_config(config: dict[str, Any], registry: PluginRegistry | None = None) -> list[str]:
  """Validate graph config structure and optionally plugin existence."""
  issues: list[str] = []
  if not _is_non_empty_string(config.get("seed_topic")):
    issues.append("seed_topic must be a non-empty string")
  if not _is_non_empty_string(config.get("seed_schema")):
    issues.append("seed_schema must be a non-empty string")
  nodes = config.get("nodes")
  if not isinstance(nodes, list) or len(nodes) == 0:
    issues.append("nodes must be a non-empty list")
    return issues
  for idx, node_cfg in enumerate(nodes):
    prefix = f"nodes[{idx}]"
    if not isinstance(node_cfg, dict):
      issues.append(f"{prefix} must be a mapping")
      continue
    plugin_name = node_cfg.get("plugin")
    if not _is_non_empty_string(plugin_name):
      issues.append(f"{prefix}.plugin must be a non-empty string")
      continue
    topics = node_cfg.get("input_topics")
    if topics is not None:
      if not isinstance(topics, list) or not all(_is_non_empty_string(t) for t in topics):
        issues.append(f"{prefix}.input_topics must be a list of non-empty strings")
    if registry is not None:
      try:
        registry.get_node(str(plugin_name))
      except KeyError:
        issues.append(f"{prefix}.plugin not found in registry: {plugin_name}")
  return issues


def wire_graph_from_config(runtime: PipelineRuntime, registry: PluginRegistry, config: dict[str, Any]) -> None:
  nodes = config.get("nodes", [])
  for node_cfg in nodes:
    plugin_name = str(node_cfg["plugin"])
    node_name = str(node_cfg.get("name", plugin_name))
    node = registry.get_node(plugin_name)
    topics = node_cfg.get("input_topics", node.input_topics)
    for topic in topics:
      runtime.subscribe(node_name=node_name, topic=str(topic), handler=node.handle)
