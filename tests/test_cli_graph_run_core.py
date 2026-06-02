from pathlib import Path

from typer.testing import CliRunner

from robot_core.cli import app


def test_graph_run_core_writes_all_core_pack_traces(tmp_path: Path) -> None:
  runner = CliRunner()
  output_dir = tmp_path / "core_graph_runs"
  result = runner.invoke(app, ["graph-run-core", "--output-dir", str(output_dir)])
  assert result.exit_code == 0
  assert "core_graph_runs_ok packs=3" in result.stdout
  assert "total_messages=9" in result.stdout
  assert (output_dir / "surveillance_trace.jsonl").exists()
  assert (output_dir / "delivery_trace.jsonl").exists()
  assert (output_dir / "warehouse_trace.jsonl").exists()
  for path in (
    output_dir / "surveillance_trace.jsonl",
    output_dir / "delivery_trace.jsonl",
    output_dir / "warehouse_trace.jsonl",
  ):
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 3


def test_graph_list_core_prints_bundled_config_paths() -> None:
  runner = CliRunner()
  result = runner.invoke(app, ["graph-list-core"])
  assert result.exit_code == 0
  assert "examples/robot_packs/surveillance.yaml" in result.stdout
  assert "examples/robot_packs/delivery.yaml" in result.stdout
  assert "examples/robot_packs/warehouse.yaml" in result.stdout
  assert "core_graph_count=3" in result.stdout
