from pathlib import Path

from robot_core.builtin_plugins import register_builtin_plugins
from robot_core.graph import load_graph_config, validate_graph_config
from robot_core.plugins import PluginRegistry


def test_graph_validation_accepts_surveillance_pack() -> None:
  reg = PluginRegistry()
  register_builtin_plugins(reg)
  cfg = load_graph_config(Path("examples/robot_packs/surveillance.yaml"))
  assert validate_graph_config(cfg, registry=reg) == []


def test_graph_validation_reports_missing_plugin() -> None:
  reg = PluginRegistry()
  register_builtin_plugins(reg)
  cfg = {
    "seed_topic": "sensors.bundle",
    "seed_schema": "SensorBundle",
    "nodes": [{"plugin": "does_not_exist", "input_topics": ["sensors.bundle"]}],
  }
  issues = validate_graph_config(cfg, registry=reg)
  assert any("plugin not found in registry" in issue for issue in issues)


def test_graph_validation_reports_missing_nodes_list() -> None:
  reg = PluginRegistry()
  register_builtin_plugins(reg)
  cfg = load_graph_config(Path("tests/fixtures/graph/invalid_missing_nodes.yaml"))
  issues = validate_graph_config(cfg, registry=reg)
  assert "nodes must be a non-empty list" in issues
