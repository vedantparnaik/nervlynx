from __future__ import annotations

import time
from pathlib import Path

import typer

from robot_core.builtin_plugins import register_builtin_plugins
from robot_core.checkpoint import CheckpointStore
from robot_core.chaos import ChaosConfig, run_chaos_pass
from robot_core.codegen import run_codegen
from robot_core.contracts import check_contract_migration, default_contracts
from robot_core.dashboard import serve_dashboard
from robot_core.distributed import DistributedNodeConfig, DistributedNodeRunner
from robot_core.examples import build_reference_runtime
from robot_core.graph import load_graph_config, wire_graph_from_config
from robot_core.metrics import MetricsRegistry, serve_metrics
from robot_core.observability import flow_stats, topic_latency_stats
from robot_core.plugins import PluginRegistry
from robot_core.recorder import read_jsonl, write_jsonl
from robot_core.runtime import PipelineRuntime
from robot_core.security import TopicAccessPolicy, sign_payload
from robot_core.smoke_matrix import run_smoke_matrix
from robot_core.smoke_surveillance import run_surveillance_smoke
from robot_core.supervisor import ManagedNode, RuntimeSupervisor
from robot_core.transport import InMemoryTransport

app = typer.Typer(help="Generic robotics runtime skeleton CLI.")


@app.command("run-example")
def run_example(output: Path = Path("logs/robot_core_trace.jsonl")) -> None:
  rt = build_reference_runtime()
  seed = rt.publish(
    topic="sensors.raw",
    source="sensor_adapter",
    schema="RawSensors",
    payload={"camera_count": 4, "gps_fix": True},
  )
  trace = rt.run_once(seed)
  write_jsonl(output, trace)
  typer.echo(f"Wrote {len(trace)} messages to {output}")


@app.command("replay")
def replay(input: Path) -> None:
  events = read_jsonl(input)
  for msg in events:
    e = msg.envelope
    typer.echo(
      f"{e.topic} source={e.source} seq={e.sequence} schema={e.schema} trace={e.trace_id[:8]}"
    )


@app.command("smoke-surveillance")
def smoke_surveillance(output: Path = Path("logs/smoke_surveillance_trace.jsonl")) -> None:
  result = run_surveillance_smoke(output_path=output)
  status = "PASS" if result.ok else "FAIL"
  typer.echo(f"{status} messages={result.message_count} output={result.output_path}")
  typer.echo("topics=" + ",".join(result.topics))
  if result.watchdog_faults:
    typer.echo("watchdog_faults=" + "; ".join(result.watchdog_faults))


@app.command("smoke-matrix")
def smoke_matrix(output_dir: Path = Path("logs/smoke_matrix")) -> None:
  results = run_smoke_matrix(output_dir=output_dir)
  ok = all(r.ok for r in results)
  for r in results:
    typer.echo(f"{'PASS' if r.ok else 'FAIL'} case={r.name} messages={r.message_count} path={r.output_path}")
  typer.echo("overall=" + ("PASS" if ok else "FAIL"))
  raise typer.Exit(code=0 if ok else 1)


@app.command("inspect-trace")
def inspect_trace(input: Path) -> None:
  events = read_jsonl(input)
  for stat in topic_latency_stats(events):
    typer.echo(f"topic={stat.topic} count={stat.count} avg_delta_ms={stat.avg_delta_ms:.3f}")
  for stat in flow_stats(events):
    typer.echo(f"trace={stat.trace_id[:8]} messages={stat.topic_count} e2e_ms={stat.end_to_end_ms:.3f}")


@app.command("contracts-check")
def contracts_check() -> None:
  contracts = default_contracts()
  old = contracts["mission.command"]
  new = type(old)(
    topic=old.topic,
    schema=old.schema,
    version=old.version + 1,
    required_fields=old.required_fields + ("priority",),
  )
  issues = check_contract_migration(old, new)
  typer.echo("contracts_ok=" + str(len(issues) == 0).lower())
  if issues:
    for issue in issues:
      typer.echo(f"{issue.topic}: {issue.message}")


@app.command("plugin-catalog")
def plugin_catalog() -> None:
  reg = PluginRegistry()
  reg.discover_entrypoints()
  if not reg.catalog().nodes and not reg.catalog().sensors:
    register_builtin_plugins(reg)
  cat = reg.catalog()
  typer.echo(f"sensors={len(cat.sensors)} nodes={len(cat.nodes)}")


@app.command("serve-metrics")
def serve_metrics_cmd(duration_s: float = 5.0, port: int = 9108) -> None:
  reg = MetricsRegistry()
  reg.inc("nervlynx_boot_total", 1)
  reg.set_gauge("nervlynx_uptime_seconds", 0.0)
  server = serve_metrics(reg, port=port)
  started = time.monotonic()
  while time.monotonic() - started < duration_s:
    reg.set_gauge("nervlynx_uptime_seconds", time.monotonic() - started)
    time.sleep(0.2)
  server.shutdown()
  typer.echo(f"metrics_server_stopped port={port}")


@app.command("supervisor-demo")
def supervisor_demo() -> None:
  order: list[str] = []

  def mk_start(name: str):
    return lambda: order.append("start:" + name)

  def mk_stop(name: str):
    return lambda: order.append("stop:" + name)

  sup = RuntimeSupervisor()
  sup.register(ManagedNode("transport", start=mk_start("transport"), stop=mk_stop("transport")))
  sup.register(
    ManagedNode(
      "planner",
      start=mk_start("planner"),
      stop=mk_stop("planner"),
      dependencies=("transport",),
    )
  )
  sup.start_all()
  sup.stop_all()
  typer.echo(",".join(order))


@app.command("run-graph")
def run_graph(config: Path, output: Path = Path("logs/graph_trace.jsonl")) -> None:
  reg = PluginRegistry()
  reg.discover_entrypoints()
  if not reg.catalog().nodes and not reg.catalog().sensors:
    register_builtin_plugins(reg)
  runtime = PipelineRuntime(topic_priority={"safety.event": 1})
  cfg = load_graph_config(config)
  wire_graph_from_config(runtime, reg, cfg)
  seed = runtime.publish(
    topic=str(cfg.get("seed_topic", "sensors.bundle")),
    source="graph_runner",
    schema=str(cfg.get("seed_schema", "SensorBundle")),
    payload=dict(cfg.get("seed_payload", {})),
  )
  trace = runtime.run_once(seed)
  write_jsonl(output, trace)
  typer.echo(f"trace_messages={len(trace)} output={output}")


@app.command("dashboard-demo")
def dashboard_demo(duration_s: float = 5.0, port: int = 9120) -> None:
  runtime = PipelineRuntime()
  metrics = MetricsRegistry()
  server = serve_dashboard(runtime, metrics, port=port)
  started = time.monotonic()
  while time.monotonic() - started < duration_s:
    metrics.inc("nervlynx_dashboard_ticks_total", 1)
    time.sleep(0.2)
  server.shutdown()
  typer.echo(f"dashboard_stopped port={port}")


@app.command("chaos-pass")
def chaos_pass(drop_probability: float = 0.2, mutate_probability: float = 0.2) -> None:
  runtime = build_reference_runtime()
  message_count = run_chaos_pass(
    runtime,
    seed_topic="sensors.raw",
    seed_payload={"camera_count": 4, "gps_fix": True},
    cfg=ChaosConfig(drop_probability=drop_probability, mutate_probability=mutate_probability),
  )
  typer.echo(f"chaos_trace_messages={message_count}")


@app.command("checkpoint-demo")
def checkpoint_demo(node_name: str = "planner") -> None:
  store = CheckpointStore()
  store.save(node_name, {"mode": "patrol", "last_waypoint": "wp-1", "version": 1})
  loaded = store.load(node_name) or {}
  typer.echo(f"checkpoint_loaded={bool(loaded)} fields={','.join(sorted(loaded.keys()))}")


@app.command("distributed-demo")
def distributed_demo() -> None:
  transport = InMemoryTransport()
  reg = PluginRegistry()
  register_builtin_plugins(reg)
  cfg = DistributedNodeConfig(
    node_name="perception_worker",
    plugin_name="perception_node",
    subscribe_topics=("sensors.bundle",),
    policy=TopicAccessPolicy(
      allowed_publish_topics=("perception.scene",),
      allowed_subscribe_topics=("sensors.bundle",),
    ),
    secret="local-dev-secret",
  )
  runner = DistributedNodeRunner(transport=transport, registry=reg, config=cfg)
  received: list[str] = []

  def on_scene(msg):
    received.append(msg.envelope.topic)

  transport.subscribe("perception.scene", on_scene)
  runner.start()
  seed_payload = {"camera_count": 4, "gps_fix": True, "imu_ok": True, "ai_ok": True}
  signed = dict(seed_payload)
  signed["_signature"] = sign_payload(seed_payload, "local-dev-secret")
  seed = PipelineRuntime().publish(
    topic="sensors.bundle",
    source="sensor_hub",
    schema="SensorBundle",
    payload=signed,
  )
  transport.publish(seed)
  typer.echo(f"distributed_outputs={len(received)} topics={','.join(received)}")


@app.command("contracts-codegen")
def contracts_codegen() -> None:
  py_path, cpp_path = run_codegen()
  typer.echo(f"generated_python={py_path} generated_cpp={cpp_path}")


@app.command("version")
def version_cmd() -> None:
  """Print the installed nervlynx package version."""
  from importlib.metadata import PackageNotFoundError, version

  try:
    typer.echo(version("nervlynx"))
  except PackageNotFoundError:
    typer.echo("nervlynx-unknown")
