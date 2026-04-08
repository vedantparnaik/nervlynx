from __future__ import annotations

from pathlib import Path

import typer

from robot_core.contracts import check_contract_migration, default_contracts
from robot_core.examples import build_reference_runtime
from robot_core.observability import topic_latency_stats
from robot_core.plugins import PluginRegistry
from robot_core.recorder import read_jsonl, write_jsonl
from robot_core.smoke_matrix import run_smoke_matrix
from robot_core.smoke_surveillance import run_surveillance_smoke

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
  cat = reg.catalog()
  typer.echo(f"sensors={len(cat.sensors)} nodes={len(cat.nodes)}")
