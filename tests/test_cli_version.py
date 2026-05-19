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
