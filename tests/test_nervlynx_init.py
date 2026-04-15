from pathlib import Path

from typer.testing import CliRunner

from robot_core.nervlynx_cli import app


def test_nervlynx_init_scaffolds_project_tree() -> None:
  runner = CliRunner()
  with runner.isolated_filesystem():
    result = runner.invoke(app, ["init", "alpha-pack"])
    assert result.exit_code == 0
    root = Path("alpha-pack")
    assert (root / "pyproject.toml").exists()
    assert (root / "README.md").exists()
    assert (root / "alpha_pack" / "plugins.py").exists()
    assert (root / "tests" / "test_plugins.py").exists()
    assert (root / "config" / "graph.yaml").exists()


def test_nervlynx_init_requires_force_for_non_empty_target() -> None:
  runner = CliRunner()
  with runner.isolated_filesystem():
    root = Path("alpha-pack")
    root.mkdir(parents=True, exist_ok=True)
    (root / "already.txt").write_text("present", encoding="utf-8")
    result = runner.invoke(app, ["init", "alpha-pack"])
    assert result.exit_code == 1
    forced = runner.invoke(app, ["init", "alpha-pack", "--force"])
    assert forced.exit_code == 0
