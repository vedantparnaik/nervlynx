import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from robot_core.nervlynx_cli import app
from robot_core.overlay import OverlayError, apply_overlay, merge, write_calibration
from robot_core.project import load_project
from robot_core.remote import deploy, target_for

runner = CliRunner()

BASE = {
  "name": "rover",
  "hardware": {"backend": "auto"},
  "nodes": [
    {
      "name": "drive",
      "plugin": "skid_steer_drive",
      "params": {
        "driver": "l298n",
        "left": [{"name": "left", "in1": 5, "in2": 6, "en": 12}],
        "right": [{"name": "right", "in1": 20, "in2": 21, "en": 13}],
        "tuning": {"min_duty": 0.18, "kick_s": 0.18},
      },
    },
    {"plugin": "hcsr04_range", "params": {"trigger": 23, "echo": 24}},
  ],
}


def test_named_lists_merge_by_name_and_mappings_key_by_key() -> None:
  merged = merge(
    BASE["nodes"][0]["params"],
    {"right": [{"name": "right", "invert": True}], "tuning": {"min_duty": 0.25}},
  )
  assert merged["right"] == [{"name": "right", "in1": 20, "in2": 21, "en": 13, "invert": True}]
  assert merged["tuning"] == {"min_duty": 0.25, "kick_s": 0.18}
  assert merged["left"] == BASE["nodes"][0]["params"]["left"]
  assert merge([1, 2], [3]) == [3] and merge({"a": 1}, 5) == 5


def test_overlays_patch_nodes_by_name_and_fail_loudly_on_stale_names() -> None:
  cfg = apply_overlay(BASE, {"nodes": {"hcsr04_range": {"params": {"max_range_m": 1.5}}}, "hardware": {"backend": "gpiozero"}}, source="overlay.yaml")
  assert cfg["nodes"][1]["params"] == {"trigger": 23, "echo": 24, "max_range_m": 1.5}
  assert cfg["hardware"] == {"backend": "gpiozero"} and BASE["hardware"] == {"backend": "auto"}
  with pytest.raises(OverlayError, match="calibration.yaml: node 'arm' is not in robot.yaml"):
    apply_overlay(BASE, {"nodes": {"arm": {"params": {}}}}, source="calibration.yaml")
  with pytest.raises(OverlayError, match=r"nodes\.drive\.params\.left: robot.yaml has no item named 'front_left' \(it has: left\)"):
    apply_overlay(BASE, {"nodes": {"drive": {"params": {"left": [{"name": "front_left", "invert": True}]}}}}, source="calibration.yaml")
  with pytest.raises(OverlayError, match="cannot change a node's name or plugin"):
    apply_overlay(BASE, {"nodes": {"drive": {"plugin": "esp32_link"}}}, source="overlay.yaml")


def write_project(tmp_path: Path) -> Path:
  (tmp_path / "robot.yaml").write_text(yaml.safe_dump(BASE), encoding="utf-8")
  return tmp_path / "robot.yaml"


def test_projects_apply_overlay_then_calibration(tmp_path: Path) -> None:
  config = write_project(tmp_path)
  (tmp_path / "overlay.yaml").write_text("nodes: {drive: {params: {tuning: {min_duty: 0.3}, max_speed: 0.5}}}\n", encoding="utf-8")
  write_calibration(config, {"drive": {"params": {"tuning": {"min_duty": 0.22}, "left": [{"name": "left", "invert": True}]}}})
  cfg, _, problems = load_project(config)
  params = cfg["nodes"][0]["params"]
  assert problems == []
  assert params["tuning"]["min_duty"] == 0.22 and params["max_speed"] == 0.5
  assert params["left"][0]["invert"] is True
  (tmp_path / "calibration.yaml").write_text("nodes: {gone: {params: {}}}\n", encoding="utf-8")
  _, _, problems = load_project(config)
  assert problems == ["calibration.yaml: node 'gone' is not in robot.yaml (nodes: drive, hcsr04_range)"]


def test_write_calibration_keeps_earlier_entries(tmp_path: Path) -> None:
  config = write_project(tmp_path)
  write_calibration(config, {"drive": {"params": {"left": [{"name": "left", "invert": True}]}}})
  path = write_calibration(config, {"drive": {"params": {"right": [{"name": "right", "invert": True}], "left": [{"name": "left", "invert": False}]}}})
  text = path.read_text(encoding="utf-8")
  assert text.startswith("# Written by the NervLynx calibration wizard")
  saved = yaml.safe_load(text)["nodes"]["drive"]["params"]
  assert saved == {"left": [{"name": "left", "invert": False}], "right": [{"name": "right", "invert": True}]}
  assert not list(tmp_path.glob(".calibration-*"))


def test_validate_and_runs_report_the_overlays_they_used(tmp_path: Path) -> None:
  config = write_project(tmp_path)
  write_calibration(config, {"drive": {"params": {"right": [{"name": "right", "invert": True}]}}})
  checked = runner.invoke(app, ["validate", str(config)])
  assert checked.exit_code == 0 and "(2 nodes, with calibration.yaml)" in checked.stdout
  run_dir = tmp_path / "run"
  result = runner.invoke(app, ["run", str(config), "--duration-s", "0.2", "--no-server", "--quiet", "--run-dir", str(run_dir)])
  assert result.exit_code == 0, result.stdout
  assert (run_dir / "calibration.yaml").exists()
  report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
  assert report["artifacts"]["calibration"] == str(run_dir / "calibration.yaml")


class Recorder:
  def __init__(self):
    self.calls: list[list[str]] = []

  def __call__(self, argv):
    self.calls.append(list(argv))
    return 0


def test_deploy_never_touches_a_robots_calibration(tmp_path: Path) -> None:
  write_project(tmp_path)
  rec = Recorder()
  deploy(target_for("pi@rover.local", tmp_path), tmp_path, validate=False, runner=rec, echo=lambda line: None)
  rsync = rec.calls[1]
  assert "--exclude=/calibration.yaml" in rsync and "--exclude=/overlay.yaml" in rsync
