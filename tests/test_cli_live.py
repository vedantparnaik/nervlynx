import json
from pathlib import Path

from typer.testing import CliRunner

from robot_core.cli import _build_plugin_registry, app
from robot_core.live import LiveRuntime
from robot_core.live_config import build_live_runtime, load_live_config
from robot_core.recorder import read_jsonl
from robot_core.runtime import SimulatedClock
from robot_core.server import serve_live

runner = CliRunner()


def test_run_live_sim_time_writes_a_complete_run_directory(tmp_path: Path) -> None:
  run_dir = tmp_path / "run"
  result = runner.invoke(
    app,
    ["run-live", "examples/live/rover_sim.yaml", "--sim-time", "--duration-s", "8", "--run-dir", str(run_dir), "--strict"],
  )
  assert result.exit_code == 0, result.stdout
  assert "run_live_done graph=rover_sim status=ok" in result.stdout
  assert "# NervLynx run report: rover_sim" in result.stdout
  for name in ("config.yaml", "trace.jsonl", "faults.jsonl", "report.json", "report.md", "metrics.prom"):
    assert (run_dir / name).exists(), name
  report = json.loads((run_dir / "report.json").read_text())
  assert report["clock"] == "simulated"
  assert report["runtime_duration_s"] == 8.0
  assert report["trace_messages"] == report["messages"]["published"]
  assert report["nodes"]["drive"]["ticks"] == 401
  assert report["topics"]["cmd.drive"]["count"] == 161
  trace = read_jsonl(run_dir / "trace.jsonl")
  assert len(trace) == report["trace_messages"]
  assert "nervlynx_motor_duty" in (run_dir / "metrics.prom").read_text()


def test_run_live_is_deterministic_in_sim_time(tmp_path: Path) -> None:
  outputs = []
  for idx in range(2):
    run_dir = tmp_path / f"run{idx}"
    result = runner.invoke(app, ["run-live", "examples/live/rover_sim.yaml", "--sim-time", "--duration-s", "3", "--run-dir", str(run_dir), "--quiet"])
    assert result.exit_code == 0
    outputs.append((run_dir / "trace.jsonl").read_text())
  assert outputs[0] == outputs[1]


def test_run_live_record_exclude_and_no_record(tmp_path: Path) -> None:
  result = runner.invoke(
    app,
    ["run-live", "examples/live/rover_sim.yaml", "--sim-time", "--duration-s", "2", "--run-dir", str(tmp_path / "a"), "--record-exclude", "drive.state", "--quiet"],
  )
  assert result.exit_code == 0
  topics = {m.envelope.topic for m in read_jsonl(tmp_path / "a" / "trace.jsonl")}
  assert "drive.state" not in topics and "cmd.drive" in topics
  result = runner.invoke(app, ["run-live", "examples/live/rover_sim.yaml", "--sim-time", "--duration-s", "1", "--run-dir", str(tmp_path / "b"), "--no-record", "--quiet"])
  assert result.exit_code == 0
  assert not (tmp_path / "b" / "trace.jsonl").exists()


def test_run_live_requires_duration_in_sim_time_and_rejects_bad_configs(tmp_path: Path) -> None:
  result = runner.invoke(app, ["run-live", "examples/live/rover_sim.yaml", "--sim-time"])
  assert result.exit_code == 2
  bad = tmp_path / "bad.yaml"
  bad.write_text("nodes:\n  - plugin: nope\n")
  result = runner.invoke(app, ["run-live", str(bad), "--sim-time", "--duration-s", "1"])
  assert result.exit_code == 1
  assert "plugin not found in registry: nope" in result.stdout


def test_strict_mode_fails_when_the_estop_latches(tmp_path: Path) -> None:
  cfg = tmp_path / "estop.yaml"
  cfg.write_text(Path("examples/live/rover_sim.yaml").read_text().replace("safety:\n  stale_after_s: 0.5", "safety:\n  start_in_estop: true"))
  result = runner.invoke(app, ["run-live", str(cfg), "--sim-time", "--duration-s", "1", "--run-dir", str(tmp_path / "r"), "--strict", "--quiet"])
  assert result.exit_code == 2
  assert "run_live_strict=fail" in result.stdout


def test_live_validate_cli(tmp_path: Path) -> None:
  result = runner.invoke(app, ["live-validate", "examples/live/rover_sim.yaml", "examples/live/rover_bts7960.yaml"])
  assert result.exit_code == 0
  assert "examples/live/rover_sim.yaml: live_config_valid=true nodes=3" in result.stdout
  bad = tmp_path / "bad.yaml"
  bad.write_text("nodes: 3\n")
  result = runner.invoke(app, ["live-validate", str(bad)])
  assert result.exit_code == 1
  assert "nodes must be a non-empty list" in result.stdout


def test_top_once_renders_a_running_graph() -> None:
  rt = build_live_runtime(load_live_config("examples/live/rover_sim.yaml"), _build_plugin_registry(), clock=SimulatedClock())
  rt.start()
  for _ in range(30):
    rt.clock.advance_ms(20)
    rt.step()
  server = serve_live(rt, port=0)
  try:
    result = runner.invoke(app, ["top", f"http://127.0.0.1:{server.server_address[1]}", "--once"])
  finally:
    server.shutdown()
    server.server_close()
  assert result.exit_code == 0
  assert "rover_sim  [OK]" in result.stdout
  for token in ("pattern", "drive", "plant", "cmd.drive", "drive.state"):
    assert token in result.stdout


def test_top_reports_unreachable_dashboard() -> None:
  result = runner.invoke(app, ["top", "http://127.0.0.1:9", "--once"])
  assert result.exit_code == 1
  assert "top_error" in result.stdout


def test_inspect_trace_limit(tmp_path: Path) -> None:
  run_dir = tmp_path / "run"
  runner.invoke(app, ["run-live", "examples/live/rover_sim.yaml", "--sim-time", "--duration-s", "2", "--run-dir", str(run_dir), "--quiet"])
  result = runner.invoke(app, ["inspect-trace", str(run_dir / "trace.jsonl"), "--limit", "3"])
  assert result.exit_code == 0
  assert sum(1 for line in result.stdout.splitlines() if line.startswith("trace=")) == 3
  assert "traces_total=" in result.stdout


def test_chaos_pass_cli_trials_output() -> None:
  result = runner.invoke(app, ["chaos-pass", "--trials", "200", "--seed", "3"])
  assert result.exit_code == 0
  assert result.stdout.startswith("chaos_trials=200 dropped=")
  again = runner.invoke(app, ["chaos-pass", "--trials", "200", "--seed", "3"])
  assert again.stdout == result.stdout


def test_live_runtime_is_importable_from_package_root() -> None:
  import robot_core

  assert robot_core.LiveRuntime is LiveRuntime
  assert "SkidSteerDrive" in robot_core.__all__
