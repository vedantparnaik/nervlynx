from pathlib import Path

from typer.testing import CliRunner

from robot_core.cli import app


def test_graph_run_core_writes_all_core_pack_traces(tmp_path: Path) -> None:
  runner = CliRunner()
  output_dir = tmp_path / "core_graph_runs"
  result = runner.invoke(app, ["graph-run-core", "--output-dir", str(output_dir)])
  assert result.exit_code == 0
  assert "core_graph_runs_ok packs=3" in result.stdout
  assert (output_dir / "surveillance_trace.jsonl").exists()
  assert (output_dir / "delivery_trace.jsonl").exists()
  assert (output_dir / "warehouse_trace.jsonl").exists()
