"""Run a live graph as a session: dashboard, trace, fault log, and an end-of-run report.

`robot-core run-live`, `nervlynx sim`, and `nervlynx run` all go through `run_session`.
"""

from __future__ import annotations

import json
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

from robot_core.live import LiveRuntimeError
from robot_core.live_config import build_live_runtime, clock_for_config, validate_live_config
from robot_core.overlay import overlay_paths
from robot_core.project import load_project
from robot_core.report import FaultLog, TraceRecorder, build_report, render_markdown
from robot_core.server import serve_live

STRICT_FAULT_KINDS = ("node_error", "watchdog", "stall", "estop", "setup_failed")


@dataclass
class SessionOptions:
  mode: str = "robot"
  duration_s: float | None = None
  sim_time: bool = False
  backend: str | None = None
  host: str = "127.0.0.1"
  port: int = 9120
  no_server: bool = False
  allow_control: bool = False
  control_topics: Sequence[str] = ("cmd.drive",)
  control_token: str | None = None
  run_dir: Path | None = None
  no_record: bool = False
  record_exclude: Sequence[str] = ()
  strict: bool = False
  quiet: bool = False
  fail_on_collision: bool = False
  device: str | None = None


def run_session(config: Path, opts: SessionOptions, echo: Callable[[str], Any]) -> int:
  """Run `config` until the duration elapses or SIGINT/SIGTERM; return the process exit code.

  In `sim` mode every hardware node is forced onto mock pins unless `opts.backend` says
  otherwise, and only nodes marked `only: sim` (or unmarked) run. When the config lists
  `devices`, robot mode runs this device's nodes (from `opts.device` or the hostname) and
  connects them to the others; sim mode runs every node here unless a device is given.
  """
  backend = opts.backend or ("mock" if opts.mode == "sim" else None)
  try:
    cfg, reg, problems = load_project(config)
  except (OSError, ValueError, yaml.YAMLError) as exc:
    echo(f"{config}: config_error: {exc}")
    return 1
  issues = problems + validate_live_config(cfg, reg, backend_override=backend, mode=opts.mode)
  if issues:
    for issue in issues:
      echo(f"{config}: config_error: {issue}")
    return 1
  clock = clock_for_config(cfg, simulated=opts.sim_time)
  if clock.simulated and opts.duration_s is None:
    echo("config_error: --duration-s is required with a simulated clock")
    return 2
  device = opts.device
  if cfg.get("devices"):
    from robot_core.mesh import MeshError, detect_device

    try:
      if device is None and opts.mode == "robot":
        device = detect_device(cfg)
      runtime = build_live_runtime(cfg, reg, clock=clock, backend_override=backend, mode=opts.mode, device=device)
    except MeshError as exc:
      echo(f"{config}: config_error: {exc}")
      return 1
  elif device is not None:
    echo(f"{config}: config_error: --device needs a devices: section in the config")
    return 1
  else:
    runtime = build_live_runtime(cfg, reg, clock=clock, backend_override=backend, mode=opts.mode)

  out_dir = opts.run_dir or Path("logs/live") / f"{runtime.name}-{time.strftime('%Y%m%d-%H%M%S')}"
  out_dir.mkdir(parents=True, exist_ok=True)
  shutil.copyfile(config, out_dir / "config.yaml")
  overlays = overlay_paths(config)
  for path in overlays:
    shutil.copyfile(path, out_dir / path.name)
  if overlays:
    echo(f"overlays={','.join(path.name for path in overlays)}")
  fault_log = FaultLog(out_dir / "faults.jsonl")
  runtime.add_fault_listener(fault_log)
  recorder = None
  if not opts.no_record:
    recorder = TraceRecorder(out_dir / "trace.jsonl", exclude_topics=list(opts.record_exclude))
    runtime.add_message_listener(recorder)

  server = None
  if not opts.no_server and not clock.simulated:
    try:
      server = serve_live(
        runtime,
        host=opts.host,
        port=opts.port,
        allow_control=opts.allow_control,
        control_topics=list(opts.control_topics),
        control_token=opts.control_token,
        config_path=config,
      )
    except OSError as exc:
      fault_log.close()
      if recorder is not None:
        recorder.close()
      echo(f"dashboard_error: cannot listen on {opts.host}:{opts.port}: {exc}")
      return 1
    shown = "127.0.0.1" if opts.host in ("0.0.0.0", "") else opts.host
    echo(f"dashboard=http://{shown}:{opts.port}/ metrics=http://{shown}:{opts.port}/metrics control={'on' if opts.allow_control else 'off'}")

  previous: dict[int, Any] = {}
  for sig in (signal.SIGINT, signal.SIGTERM):
    try:
      previous[sig] = signal.signal(sig, lambda *_: runtime.stop())
    except ValueError:  # not on the main thread
      pass
  where = f" device={device}" if device else ""
  echo(f"run_live_started graph={runtime.name} mode={opts.mode}{where} clock={'simulated' if clock.simulated else 'system'} run_dir={out_dir}")
  wall_started = time.time()
  exit_code = 0
  try:
    runtime.run(duration_s=opts.duration_s)
  except LiveRuntimeError as exc:
    echo(f"run_live_error: {exc}")
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
  for path in overlays:
    artifacts[path.stem] = str(out_dir / path.name)
  if recorder is not None:
    artifacts["trace"] = str(recorder.path)
  artifacts.update({"report_json": str(out_dir / "report.json"), "report_md": str(out_dir / "report.md"), "metrics": str(out_dir / "metrics.prom")})
  report = build_report(
    runtime,
    config_path=config,
    wall_started=wall_started,
    wall_finished=wall_finished,
    artifacts=artifacts,
    extra={"trace_messages": recorder.written if recorder else 0, "fault_log_entries": fault_log.count, "mode": opts.mode, "device": device},
  )
  (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
  markdown = render_markdown(report)
  (out_dir / "report.md").write_text(markdown, encoding="utf-8")
  (out_dir / "metrics.prom").write_text(runtime.metrics.render_prometheus(), encoding="utf-8")
  if not opts.quiet:
    echo(markdown)
  health = report["health"]["status"]
  echo(f"run_live_done graph={runtime.name} status={health} messages={report['messages']['published']} report={out_dir / 'report.json'}")
  if exit_code == 0 and opts.strict:
    kinds = report["faults"]["by_kind"]
    strict_kinds = STRICT_FAULT_KINDS + (("sim_collision",) if opts.fail_on_collision else ())
    if any(kinds.get(kind) for kind in strict_kinds):
      echo("run_live_strict=fail")
      exit_code = 2
  return exit_code
