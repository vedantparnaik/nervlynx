from pathlib import Path
from urllib.request import urlopen

from robot_core.builtin_plugins import register_builtin_plugins
from robot_core.graph import load_graph_config, wire_graph_from_config
from robot_core.metrics import MetricsRegistry, serve_metrics
from robot_core.plugins import PluginRegistry
from robot_core.runtime import PipelineRuntime
from robot_core.supervisor import ManagedNode, RuntimeSupervisor
from robot_core.transport import InMemoryTransport


def test_inmemory_transport_pub_sub() -> None:
  transport = InMemoryTransport()
  rt = PipelineRuntime()
  received = []

  def cb(msg):
    received.append(msg.envelope.topic)

  transport.subscribe("seed", cb)
  msg = rt.publish(topic="seed", source="src", schema="Seed", payload={})
  transport.publish(msg)
  assert received == ["seed"]


def test_supervisor_dependency_order() -> None:
  events: list[str] = []
  sup = RuntimeSupervisor()
  sup.register(ManagedNode("transport", start=lambda: events.append("start:transport"), stop=lambda: events.append("stop:transport")))
  sup.register(
    ManagedNode(
      "planner",
      start=lambda: events.append("start:planner"),
      stop=lambda: events.append("stop:planner"),
      dependencies=("transport",),
    )
  )
  sup.start_all()
  sup.stop_all()
  assert events == ["start:transport", "start:planner", "stop:planner", "stop:transport"]


def test_metrics_server_exposes_prometheus() -> None:
  reg = MetricsRegistry()
  reg.inc("nervlynx_events_total", 2)
  reg.set_gauge("nervlynx_queue_depth", 3)
  server = serve_metrics(registry=reg, host="127.0.0.1", port=9119)
  try:
    body = urlopen("http://127.0.0.1:9119/metrics", timeout=2).read().decode("utf-8")
  finally:
    server.shutdown()
  assert "nervlynx_events_total 2.0" in body
  assert "nervlynx_queue_depth 3" in body


def test_graph_loader_wires_builtin_plugins() -> None:
  reg = PluginRegistry()
  register_builtin_plugins(reg)
  rt = PipelineRuntime()
  cfg = load_graph_config(Path("deploy/config/graph_surveillance.yaml"))
  wire_graph_from_config(rt, reg, cfg)
  seed = rt.publish(
    topic=str(cfg["seed_topic"]),
    source="test",
    schema=str(cfg["seed_schema"]),
    payload=dict(cfg["seed_payload"]),
  )
  trace = rt.run_once(seed)
  assert "mission.command" in [m.envelope.topic for m in trace]
