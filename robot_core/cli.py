from __future__ import annotations

from pathlib import Path

import typer

from robot_core.examples import build_reference_runtime
from robot_core.recorder import read_jsonl, write_jsonl
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
