from __future__ import annotations

from pathlib import Path

import typer

from shuttle.broker import run_broker
from shuttle.config import load_config
from shuttle.processes import (
  run_controller,
  run_logger,
  run_planner,
  run_replay,
  run_route_manager,
  run_safety_manager,
  run_state_estimator,
  run_watchdog,
)

app = typer.Typer(help="Fixed-route shuttle ADAS stack CLI.")


@app.command("run-broker")
def cmd_broker() -> None:
  cfg = load_config()
  run_broker(cfg.publisher_address, cfg.subscriber_address)


@app.command("run-state")
def cmd_state(
  sensor_mode: str = typer.Option("synthetic", help="synthetic or replay"),
  replay_path: Path | None = typer.Option(None, help="Path to replay sensor jsonl"),
) -> None:
  if sensor_mode not in {"synthetic", "replay"}:
    raise typer.BadParameter("sensor-mode must be one of: synthetic, replay")
  run_state_estimator(sensor_mode=sensor_mode, replay_path=replay_path)


@app.command("run-planner")
def cmd_planner() -> None:
  run_planner()


@app.command("run-controller")
def cmd_controller() -> None:
  run_controller()


@app.command("run-watchdog")
def cmd_watchdog() -> None:
  run_watchdog()


@app.command("run-route-manager")
def cmd_route_manager(route: Path = typer.Option(Path("configs/route_loop.yaml"), help="Path to route yaml")) -> None:
  run_route_manager(route)


@app.command("run-safety-manager")
def cmd_safety_manager() -> None:
  run_safety_manager()


@app.command("run-logger")
def cmd_logger(output: Path = Path("logs/session.jsonl")) -> None:
  run_logger(output)


@app.command("replay")
def cmd_replay(input: Path, mode: str = "realtime") -> None:
  if mode not in {"realtime", "fast"}:
    raise typer.BadParameter("mode must be one of: realtime, fast")
  run_replay(input, mode=mode)
