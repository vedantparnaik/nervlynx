import json
from pathlib import Path

from typer.testing import CliRunner

from robot_core.nervlynx_cli import app

runner = CliRunner()


def new_project(tmp_path: Path, name: str = "my-rover", *extra: str) -> Path:
  result = runner.invoke(app, ["new", name, "--dir", str(tmp_path), *extra])
  assert result.exit_code == 0, result.stdout
  return tmp_path / name


def test_new_lists_templates_and_needs_a_name() -> None:
  listing = runner.invoke(app, ["new", "--list"])
  assert listing.exit_code == 0
  assert "obstacle-avoider" in listing.stdout and "teleop" in listing.stdout
  bare = runner.invoke(app, ["new"])
  assert bare.exit_code == 2 and "usage: nervlynx new <name>" in bare.stdout


def test_new_creates_a_named_project_and_protects_existing_folders(tmp_path: Path) -> None:
  root = new_project(tmp_path)
  assert (root / "nodes" / "avoid.py").exists()
  assert (root / "robot.yaml").read_text(encoding="utf-8").startswith("name: my-rover\n")
  assert (root / "README.md").read_text(encoding="utf-8").startswith("# my-rover\n")
  again = runner.invoke(app, ["new", "my-rover", "--dir", str(tmp_path)])
  assert again.exit_code == 1 and "already exists and is not empty" in again.stdout
  assert runner.invoke(app, ["new", "my-rover", "--dir", str(tmp_path), "--force"]).exit_code == 0
  bad_name = runner.invoke(app, ["new", "My Rover", "--dir", str(tmp_path)])
  assert bad_name.exit_code == 1 and "must start with a lowercase letter" in bad_name.stdout
  bad_template = runner.invoke(app, ["new", "x", "--template", "hovercraft", "--dir", str(tmp_path)])
  assert bad_template.exit_code == 1 and "choose one of: obstacle-avoider, teleop, follow-me" in bad_template.stdout


def test_obstacle_avoider_template_validates_and_drives_a_minute_without_collisions(tmp_path: Path) -> None:
  root = new_project(tmp_path)
  checked = runner.invoke(app, ["validate", str(root / "robot.yaml")])
  assert checked.exit_code == 0 and "ok in sim and robot modes (4 nodes)" in checked.stdout
  run_dir = tmp_path / "run"
  result = runner.invoke(
    app, ["sim", str(root / "robot.yaml"), "--fast", "--duration-s", "60", "--strict", "--quiet", "--run-dir", str(run_dir)]
  )
  assert result.exit_code == 0, result.stdout
  report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
  sim = report["nodes"]["sim"]["status"]
  assert sim["collisions"] == 0 and sim["distance_m"] > 5.0
  assert report["mode"] == "sim" and "front_range" not in report["nodes"]


def test_run_on_a_laptop_uses_mock_pins_and_robot_only_nodes(tmp_path: Path) -> None:
  root = new_project(tmp_path)
  run_dir = tmp_path / "run"
  result = runner.invoke(app, ["run", str(root / "robot.yaml"), "--duration-s", "0.3", "--no-server", "--quiet", "--run-dir", str(run_dir)])
  assert result.exit_code == 0, result.stdout
  report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
  assert report["mode"] == "robot" and "sim" not in report["nodes"]
  assert report["nodes"]["front_range"]["status"]["backend"] == "mock"
  assert report["nodes"]["drive"]["status"]["backend"] == "mock"


def test_teleop_template_simulates_cleanly(tmp_path: Path) -> None:
  root = new_project(tmp_path, "driver", "--template", "teleop")
  assert runner.invoke(app, ["validate", str(root / "robot.yaml")]).exit_code == 0
  result = runner.invoke(app, ["sim", str(root / "robot.yaml"), "--fast", "--duration-s", "5", "--strict", "--quiet", "--run-dir", str(tmp_path / "run")])
  assert result.exit_code == 0, result.stdout


def test_follow_me_template_keeps_a_walking_person_in_view(tmp_path: Path) -> None:
  root = new_project(tmp_path, "buddy", "--template", "follow-me")
  checked = runner.invoke(app, ["validate", str(root / "robot.yaml")])
  assert checked.exit_code == 0 and "ok in sim and robot modes (5 nodes)" in checked.stdout
  run_dir = tmp_path / "run"
  result = runner.invoke(app, ["sim", str(root / "robot.yaml"), "--fast", "--duration-s", "120", "--strict", "--quiet", "--run-dir", str(run_dir)])
  assert result.exit_code == 0, result.stdout
  frames = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
  views = [bool(m["payload"]["detections"]) for m in frames if m["topic"] == "detections.front"]
  assert len(views) > 1000 and sum(views) / len(views) > 0.9
  report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
  assert report["nodes"]["sim"]["status"]["collisions"] == 0 and "detector" not in report["nodes"]


def test_commands_explain_a_missing_robot_yaml(tmp_path: Path, monkeypatch) -> None:
  monkeypatch.chdir(tmp_path)
  for command in ("sim", "run", "validate"):
    result = runner.invoke(app, [command])
    assert result.exit_code == 1
    assert "robot.yaml not found. Create a project with: nervlynx new my-robot" in result.stdout
