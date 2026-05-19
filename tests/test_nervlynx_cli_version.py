from typer.testing import CliRunner

from robot_core.nervlynx_cli import app


def test_nervlynx_version_prints_package_version() -> None:
  runner = CliRunner()
  result = runner.invoke(app, ["version"])
  assert result.exit_code == 0
  out = result.stdout.strip()
  assert out
  assert out != "nervlynx-unknown"
