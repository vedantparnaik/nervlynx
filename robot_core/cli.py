from __future__ import annotations

import json
import shutil
import signal
import time
from pathlib import Path
from typing import Any, Optional
from urllib.error import URLError
from urllib.request import urlopen

import typer
import yaml

from robot_core.builtin_plugins import register_builtin_plugins
from robot_core.checkpoint import CheckpointStore
from robot_core.chaos import ChaosConfig, run_chaos_pass, run_chaos_trials
from robot_core.codegen import run_codegen
from robot_core.contracts import check_contract_migration, default_contracts
from robot_core.dashboard import serve_dashboard
from robot_core.distributed import DistributedNodeConfig, DistributedNodeRunner
from robot_core.examples import build_reference_runtime
from robot_core.graph import load_graph_config, validate_graph_config, wire_graph_from_config
from robot_core.live import LiveRuntimeError
from robot_core.live_config import build_live_runtime, clock_for_config, load_live_config, register_live_builtins, validate_live_config
from robot_core.metrics import MetricsRegistry, serve_metrics
from robot_core.observability import flow_stats, topic_latency_stats
from robot_core.plugins import PluginRegistry
from robot_core.recorder import read_jsonl, write_jsonl
from robot_core.reference_plugins import register_reference_plugins
from robot_core.report import FaultLog, TraceRecorder, build_report, render_markdown
from robot_core.runtime import PipelineRuntime
from robot_core.security import TopicAccessPolicy, sign_payload
from robot_core.server import serve_live
from robot_core.smoke_matrix import run_smoke_matrix
from robot_core.smoke_surveillance import run_surveillance_smoke
from robot_core.supervisor import ManagedNode, RuntimeSupervisor
from robot_core.transport import InMemoryTransport

app = typer.Typer(help="Generic robotics runtime skeleton CLI.")
CORE_GRAPH_CONFIGS: tuple[Path, ...] = (
  Path("examples/robot_packs/surveillance.yaml"),
  Path("examples/robot_packs/delivery.yaml"),
  Path("examples/robot_packs/warehouse.yaml"),
)


def _build_plugin_registry() -> PluginRegistry:
  reg = PluginRegistry()
  reg.discover_entrypoints()
  if not reg.catalog().nodes and not reg.catalog().sensors:
    register_builtin_plugins(reg)
    register_reference_plugins(reg)
  register_live_builtins(reg)
  return reg


def _validate_graph_paths(configs: list[Path] | tuple[Path, ...], reg: PluginRegistry) -> bool:
  has_errors = False
  for config in configs:
    cfg = load_graph_config(config)
    issues = validate_graph_config(cfg, registry=reg)
    if issues:
      has_errors = True
      for issue in issues:
        typer.echo(f"{config}: config_error: {issue}")
      continue
    typer.echo(f"{config}: graph_config_valid=true")
  return not has_errors


def _run_graph_config(config_path: Path, output: Path, reg: PluginRegistry) -> int:
  runtime = PipelineRuntime(topic_priority={"safety.event": 1})
  cfg = load_graph_config(config_path)
  issues = validate_graph_config(cfg, registry=reg)
  if issues:
    for issue in issues:
      typer.echo(f"{config_path}: config_error: {issue}")
    raise typer.Exit(code=1)
  wire_graph_from_config(runtime, reg, cfg)
  seed = runtime.publish(
    topic=str(cfg.get("seed_topic", "sensors.bundle")),
    source="graph_runner",
    schema=str(cfg.get("seed_schema", "SensorBundle")),
    payload=dict(cfg.get("seed_payload", {})),
  )
  trace = runtime.run_once(seed)
  write_jsonl(output, trace)
  return len(trace)


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
def inspect_trace(
  input: Path,
  limit: int = typer.Option(0, "--limit", help="Only print the N slowest traces (0 prints all)."),
) -> None:
  events = read_jsonl(input)
  for stat in topic_latency_stats(events):
    typer.echo(f"topic={stat.topic} count={stat.count} avg_delta_ms={stat.avg_delta_ms:.3f}")
  flows = flow_stats(events)
  for stat in flows[:limit] if limit > 0 else flows:
    typer.echo(f"trace={stat.trace_id[:8]} messages={stat.topic_count} e2e_ms={stat.end_to_end_ms:.3f}")
  if limit > 0 and len(flows) > limit:
    typer.echo(f"traces_total={len(flows)} shown={limit}")


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
  reg = _build_plugin_registry()
  trace_messages = _run_graph_config(config, output, reg)
  typer.echo(f"trace_messages={trace_messages} output={output}")


@app.command("graph-validate")
def graph_validate(configs: list[Path]) -> None:
  reg = _build_plugin_registry()
  if not _validate_graph_paths(configs, reg):
    raise typer.Exit(code=1)


@app.command("graph-validate-core")
def graph_validate_core() -> None:
  """Validate bundled core example pack configs."""
  reg = _build_plugin_registry()
  if not _validate_graph_paths(CORE_GRAPH_CONFIGS, reg):
    raise typer.Exit(code=1)


@app.command("graph-run-core")
def graph_run_core(output_dir: Path = Path("logs/core_graph_runs")) -> None:
  """Execute bundled core graph configs and write one trace per pack."""
  reg = _build_plugin_registry()
  output_dir.mkdir(parents=True, exist_ok=True)
  total_messages = 0
  for config in CORE_GRAPH_CONFIGS:
    output = output_dir / f"{config.stem}_trace.jsonl"
    trace_messages = _run_graph_config(config, output, reg)
    total_messages += trace_messages
    typer.echo(f"{config}: trace_messages={trace_messages} output={output}")
  typer.echo(f"core_graph_runs_ok packs={len(CORE_GRAPH_CONFIGS)} total_messages={total_messages} output_dir={output_dir}")


@app.command("graph-list-core")
def graph_list_core(
  output_format: str = typer.Option("text", "--format"),
  verify_exists: bool = typer.Option(False, "--verify-exists"),
) -> None:
  """List bundled core graph config paths."""
  config_paths = [str(config) for config in CORE_GRAPH_CONFIGS]
  missing_paths = [str(config) for config in CORE_GRAPH_CONFIGS if not config.exists()]
  if output_format == "json":
    payload: dict[str, object] = {"graphs": config_paths, "count": len(config_paths)}
    if verify_exists:
      payload["all_exist"] = len(missing_paths) == 0
      payload["missing"] = missing_paths
    typer.echo(json.dumps(payload))
    if verify_exists and missing_paths:
      raise typer.Exit(code=1)
    return
  if output_format != "text":
    typer.echo("format_error: supported values are text,json")
    raise typer.Exit(code=2)
  for config in config_paths:
    typer.echo(config)
  typer.echo(f"core_graph_count={len(config_paths)}")
  if verify_exists:
    typer.echo("core_graph_files_exist=" + str(len(missing_paths) == 0).lower())
    typer.echo(f"missing_graph_configs={len(missing_paths)}")
    if missing_paths:
      raise typer.Exit(code=1)


@app.command("graph-doctor")
def graph_doctor() -> None:
  """Verify core pack files exist and validate their graph configs."""
  missing_paths = [str(config) for config in CORE_GRAPH_CONFIGS if not config.exists()]
  if missing_paths:
    typer.echo(f"graph_doctor_ok=false missing={len(missing_paths)}")
    for path in missing_paths:
      typer.echo(f"missing: {path}")
    raise typer.Exit(code=1)
  reg = _build_plugin_registry()
  if not _validate_graph_paths(CORE_GRAPH_CONFIGS, reg):
    typer.echo("graph_doctor_ok=false validation_failed=true")
    raise typer.Exit(code=1)
  typer.echo(f"graph_doctor_ok=true packs={len(CORE_GRAPH_CONFIGS)}")


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
def chaos_pass(drop_probability: float = 0.2, mutate_probability: float = 0.2, seed: int = 7, trials: int = 1) -> None:
  """Inject drop/mutate faults into the reference pipeline (deterministic for a given seed)."""
  runtime = build_reference_runtime()
  cfg = ChaosConfig(drop_probability=drop_probability, mutate_probability=mutate_probability, seed=seed)
  seed_payload = {"camera_count": 4, "gps_fix": True}
  if trials <= 1:
    message_count = run_chaos_pass(runtime, seed_topic="sensors.raw", seed_payload=seed_payload, cfg=cfg)
    typer.echo(f"chaos_trace_messages={message_count}")
    return
  summary = run_chaos_trials(runtime, "sensors.raw", seed_payload, cfg, trials)
  typer.echo(
    f"chaos_trials={summary.trials} dropped={summary.dropped} mutated={summary.mutated} "
    f"passed={summary.passed_through} drop_rate={summary.drop_rate:.3f} mutate_rate={summary.mutate_rate:.3f} "
    f"total_messages={summary.total_messages}"
  )


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


@app.command("live-validate")
def live_validate(
  configs: list[Path],
  backend: Optional[str] = typer.Option(None, "--backend", help="Validate as if every node used this hardware backend."),
) -> None:
  """Validate live graph configs without touching hardware."""
  reg = _build_plugin_registry()
  ok = True
  for config in configs:
    try:
      cfg = load_live_config(config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
      typer.echo(f"{config}: config_error: {exc}")
      ok = False
      continue
    issues = validate_live_config(cfg, reg, backend_override=backend)
    if issues:
      ok = False
      for issue in issues:
        typer.echo(f"{config}: config_error: {issue}")
      continue
    typer.echo(f"{config}: live_config_valid=true nodes={len(cfg['nodes'])}")
  if not ok:
    raise typer.Exit(code=1)


@app.command("run-live")
def run_live(
  config: Path = typer.Argument(..., help="Live graph YAML, e.g. examples/live/rover_sim.yaml."),
  duration_s: Optional[float] = typer.Option(None, "--duration-s", help="Stop after this much runtime-clock time (default: until Ctrl-C)."),
  sim_time: bool = typer.Option(False, "--sim-time", help="Simulated clock: runs as fast as possible and is deterministic."),
  backend: Optional[str] = typer.Option(None, "--backend", help="Override the hardware backend of every node: mock, rpi_gpio, gpiozero."),
  host: str = typer.Option("127.0.0.1", "--host", help="Dashboard bind address. Use 0.0.0.0 to reach it from another machine."),
  port: int = typer.Option(9120, "--port", help="Dashboard / metrics port."),
  no_server: bool = typer.Option(False, "--no-server", help="Do not start the HTTP dashboard."),
  allow_control: bool = typer.Option(False, "--allow-control", help="Allow teleop publishing and e-stop clearing over HTTP."),
  control_topic: list[str] = typer.Option(["cmd.drive"], "--control-topic", help="Topic HTTP clients may publish to (repeatable)."),
  control_token: Optional[str] = typer.Option(None, "--control-token", envvar="NERVLYNX_CONTROL_TOKEN", help="Require this token for control requests."),
  run_dir: Optional[Path] = typer.Option(None, "--run-dir", help="Artifact directory (default: logs/live/<graph>-<timestamp>)."),
  no_record: bool = typer.Option(False, "--no-record", help="Skip the JSONL message trace."),
  record_exclude: list[str] = typer.Option([], "--record-exclude", help="Topic to leave out of the trace (repeatable)."),
  strict: bool = typer.Option(False, "--strict", help="Exit 2 if any node error, watchdog fault, stall, or e-stop occurred."),
  quiet: bool = typer.Option(False, "--quiet", help="Do not print the Markdown report at exit."),
) -> None:
  """Run a live graph continuously with dashboard, trace recording, and an end-of-run report."""
  reg = _build_plugin_registry()
  try:
    cfg = load_live_config(config)
  except (OSError, ValueError, yaml.YAMLError) as exc:
    typer.echo(f"{config}: config_error: {exc}")
    raise typer.Exit(code=1)
  issues = validate_live_config(cfg, reg, backend_override=backend)
  if issues:
    for issue in issues:
      typer.echo(f"{config}: config_error: {issue}")
    raise typer.Exit(code=1)
  clock = clock_for_config(cfg, simulated=sim_time)
  if clock.simulated and duration_s is None:
    typer.echo("config_error: --duration-s is required with a simulated clock")
    raise typer.Exit(code=2)
  runtime = build_live_runtime(cfg, reg, clock=clock, backend_override=backend)

  out_dir = run_dir or Path("logs/live") / f"{runtime.name}-{time.strftime('%Y%m%d-%H%M%S')}"
  out_dir.mkdir(parents=True, exist_ok=True)
  shutil.copyfile(config, out_dir / "config.yaml")
  fault_log = FaultLog(out_dir / "faults.jsonl")
  runtime.add_fault_listener(fault_log)
  recorder = None
  if not no_record:
    recorder = TraceRecorder(out_dir / "trace.jsonl", exclude_topics=record_exclude)
    runtime.add_message_listener(recorder)

  server = None
  if not no_server and not clock.simulated:
    try:
      server = serve_live(
        runtime,
        host=host,
        port=port,
        allow_control=allow_control,
        control_topics=control_topic,
        control_token=control_token,
      )
    except OSError as exc:
      fault_log.close()
      if recorder is not None:
        recorder.close()
      typer.echo(f"dashboard_error: cannot listen on {host}:{port}: {exc}")
      raise typer.Exit(code=1)
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    typer.echo(f"dashboard=http://{shown}:{port}/ metrics=http://{shown}:{port}/metrics control={'on' if allow_control else 'off'}")

  previous: dict[int, Any] = {}
  for sig in (signal.SIGINT, signal.SIGTERM):
    try:
      previous[sig] = signal.signal(sig, lambda *_: runtime.stop())
    except ValueError:  # not on the main thread
      pass
  typer.echo(f"run_live_started graph={runtime.name} clock={'simulated' if clock.simulated else 'system'} run_dir={out_dir}")
  wall_started = time.time()
  exit_code = 0
  try:
    runtime.run(duration_s=duration_s)
  except LiveRuntimeError as exc:
    typer.echo(f"run_live_error: {exc}")
    exit_code = 1
  finally:
    for sig, handler in previous.items():
      signal.signal(sig, handler)
    if server is not None:
      server.shutdown()
      server.server_close()
    if recorder is not None:
      recorder.close()
    fault_log.close()
  wall_finished = time.time()

  artifacts = {"config": str(out_dir / "config.yaml"), "faults": str(out_dir / "faults.jsonl")}
  if recorder is not None:
    artifacts["trace"] = str(recorder.path)
  artifacts.update({"report_json": str(out_dir / "report.json"), "report_md": str(out_dir / "report.md"), "metrics": str(out_dir / "metrics.prom")})
  report = build_report(
    runtime,
    config_path=config,
    wall_started=wall_started,
    wall_finished=wall_finished,
    artifacts=artifacts,
    extra={"trace_messages": recorder.written if recorder else 0, "fault_log_entries": fault_log.count},
  )
  (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
  markdown = render_markdown(report)
  (out_dir / "report.md").write_text(markdown, encoding="utf-8")
  (out_dir / "metrics.prom").write_text(runtime.metrics.render_prometheus(), encoding="utf-8")
  if not quiet:
    typer.echo(markdown)
  health = report["health"]["status"]
  typer.echo(f"run_live_done graph={runtime.name} status={health} messages={report['messages']['published']} report={out_dir / 'report.json'}")
  if exit_code == 0 and strict:
    kinds = report["faults"]["by_kind"]
    if any(kinds.get(k) for k in ("node_error", "watchdog", "stall", "estop", "setup_failed")):
      typer.echo("run_live_strict=fail")
      exit_code = 2
  raise typer.Exit(code=exit_code)


def _fmt_top(stats: dict[str, Any], url: str) -> str:
  def ms(summary: dict[str, Any] | None, key: str = "p95") -> str:
    value = (summary or {}).get(key)
    return "-" if value is None else f"{value:.3f}"

  health, msgs, ex = stats["health"], stats["messages"], stats["executor"]
  lines = [
    f"{stats['name']}  [{health['status'].upper()}]  up {health['uptime_s']:.1f}s  {stats['clock']} clock  {url}",
    f"messages pub={msgs['published']} del={msgs['delivered']} drop={msgs['dropped']}  queue={ex['queue_depth']}  "
    f"steps={ex['steps']}  e-stops={stats['estop']['events']}  stalls={ex['stalls']}",
  ]
  if stats["estop"]["engaged"]:
    lines.append(f"E-STOP LATCHED by {stats['estop']['source']}: {stats['estop']['reason']}")
  lines += ["", f"{'node':<22}{'Hz':>7}{'ticks':>9}{'handled':>9}{'err':>6}{'tick p95':>10}{'late p95':>10}{'hdl p95':>9}  state"]
  for name, node in stats["nodes"].items():
    state = "STALE" if node["stale"] else "BREAKER" if node["breaker_open"] else "ok"
    lines.append(
      f"{name[:21]:<22}{node['rate_hz'] or '':>7}{node['ticks']:>9}{node['handled']:>9}{node['errors']:>6}"
      f"{ms(node.get('tick_ms')):>10}{ms(node.get('lateness_ms')):>10}{ms(node['handler_ms']):>9}  {state}"
    )
  lines += ["", f"{'topic':<26}{'count':>9}{'Hz':>8}{'lat p50':>10}{'lat p95':>10}  last"]
  for topic, t in stats["topics"].items():
    last = json.dumps(t["last"], default=str)
    lines.append(f"{topic[:25]:<26}{t['count']:>9}{t['rate_hz']:>8.1f}{ms(t['latency_ms'], 'p50'):>10}{ms(t['latency_ms']):>10}  {last[:60]}")
  if stats["faults"]:
    lines += ["", "recent faults:"]
    lines += [f"  {f['severity']:<8} {f['kind']:<18} {f['message']}" for f in stats["faults"][-6:]]
  return "\n".join(lines)


@app.command("top")
def top(
  url: str = typer.Argument("http://127.0.0.1:9120", help="Base URL of a run-live dashboard."),
  interval_s: float = typer.Option(1.0, "--interval-s"),
  once: bool = typer.Option(False, "--once", help="Print one snapshot and exit."),
) -> None:
  """Terminal view of a running graph (handy over SSH on the robot)."""
  base = url.rstrip("/")
  while True:
    try:
      with urlopen(f"{base}/stats", timeout=3) as resp:
        stats = json.loads(resp.read().decode("utf-8"))
    except (URLError, OSError, ValueError) as exc:
      typer.echo(f"top_error: cannot read {base}/stats: {exc}")
      raise typer.Exit(code=1)
    text = _fmt_top(stats, base)
    if once:
      typer.echo(text)
      return
    typer.echo("\x1b[2J\x1b[H" + text)
    try:
      time.sleep(interval_s)
    except KeyboardInterrupt:
      return


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
