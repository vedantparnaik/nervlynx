from typer.testing import CliRunner

from robot_core.cli import app


def test_robot_core_version_prints_pep440_string() -> None:
  runner = CliRunner()
  result = runner.invoke(app, ["version"])
  assert result.exit_code == 0
  out = result.stdout.strip()
  assert out
  assert out != "nervlynx-unknown"


def test_graph_validate_command_accepts_surveillance_pack() -> None:
  runner = CliRunner()
  result = runner.invoke(app, ["graph-validate", "examples/robot_packs/surveillance.yaml"])
  assert result.exit_code == 0
  assert "graph_config_valid=true" in result.stdout


def test_graph_validate_command_rejects_invalid_fixture() -> None:
  runner = CliRunner()
  result = runner.invoke(app, ["graph-validate", "tests/fixtures/graph/invalid_missing_nodes.yaml"])
  assert result.exit_code == 1
  assert "config_error: nodes must be a non-empty list" in result.stdout


def test_graph_validate_command_accepts_multiple_valid_configs() -> None:
  runner = CliRunner()
  result = runner.invoke(
    app,
    [
      "graph-validate",
      "examples/robot_packs/surveillance.yaml",
      "examples/robot_packs/delivery.yaml",
    ],
  )
  assert result.exit_code == 0
  assert "examples/robot_packs/surveillance.yaml: graph_config_valid=true" in result.stdout
  assert "examples/robot_packs/delivery.yaml: graph_config_valid=true" in result.stdout


def test_graph_validate_command_reports_errors_for_any_invalid_config() -> None:
  runner = CliRunner()
  result = runner.invoke(
    app,
    [
      "graph-validate",
      "examples/robot_packs/surveillance.yaml",
      "tests/fixtures/graph/invalid_missing_nodes.yaml",
    ],
  )
  assert result.exit_code == 1
  assert "examples/robot_packs/surveillance.yaml: graph_config_valid=true" in result.stdout
  assert "tests/fixtures/graph/invalid_missing_nodes.yaml: config_error:" in result.stdout
