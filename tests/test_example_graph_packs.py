from pathlib import Path

from robot_core.builtin_plugins import register_builtin_plugins
from robot_core.graph import load_graph_config, wire_graph_from_config
from robot_core.plugins import PluginRegistry
from robot_core.runtime import PipelineRuntime


def test_surveillance_robot_pack_graph_runs() -> None:
  reg = PluginRegistry()
  register_builtin_plugins(reg)
  rt = PipelineRuntime()
  cfg = load_graph_config(Path("examples/robot_packs/surveillance.yaml"))
  wire_graph_from_config(rt, reg, cfg)
  seed = rt.publish(
    topic=str(cfg["seed_topic"]),
    source="test",
    schema=str(cfg["seed_schema"]),
    payload=dict(cfg["seed_payload"]),
  )
  trace = rt.run_once(seed)
  topics = [m.envelope.topic for m in trace]
  assert cfg["seed_topic"] in topics
  assert "mission.command" in topics
