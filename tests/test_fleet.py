import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from robot_core import fleet as fleet_mod
from robot_core.fleet import Fleet, Robot, SshShell, is_healthy, load_fleet, overlay_for, render, select
from robot_core.nervlynx_cli import app
from robot_core.remote import RemoteError


class FakeRobot:
  """One robot's disk and service, driven the way SshShell would drive a real one."""

  def __init__(self, name: str, *, release: str | None = "old", healthy=lambda files: True, valid=True):
    self.name = name
    self.files: dict[str, str] = {}
    self.snapshots: dict[str, dict[str, str]] = {}
    if release:
      self.files[".nervlynx-release.json"] = json.dumps({"id": release})
      self.snapshots[release] = dict(self.files)
    self.healthy = healthy
    self.valid = valid
    self.restarts = 0
    self.upgraded: list[str] = []
    self.log: list[str] = []

  def release(self):
    raw = self.files.get(".nervlynx-release.json")
    return json.loads(raw) if raw else None

  def snapshot(self, release_id):
    self.log.append(f"snapshot {release_id}")
    self.snapshots[release_id] = dict(self.files)

  def releases(self):
    return sorted(self.snapshots)

  def sync(self, project_dir: Path):
    self.log.append("sync")
    self.files["robot.yaml"] = (project_dir / "robot.yaml").read_text(encoding="utf-8")

  def write(self, name, text):
    self.log.append(f"write {name}")
    if text is None:
      self.files.pop(name, None)
    else:
      self.files[name] = text

  def validate(self):
    return self.valid, "" if self.valid else "nodes[0]: plugin not found"

  def restart(self):
    self.restarts += 1

  def probe(self):
    ok = self.healthy(self.files)
    return {"service": "active", "health": "ok" if ok else "fault", "stale": [] if ok else ["drive"], "release": self.release(), "version": "0.2.0"}

  def restore(self, release_id):
    self.log.append(f"restore {release_id}")
    self.files = dict(self.snapshots[release_id])

  def prune(self, keep):
    self.log.append(f"prune {keep}")

  def upgrade(self, spec):
    self.upgraded.append(spec)
    return True, "Successfully installed nervlynx"


def project(tmp_path: Path, mesh: bool = False) -> Path:
  root = tmp_path / "rover"
  (root / "fleet").mkdir(parents=True)
  cfg = {"name": "rover", "nodes": [{"plugin": "scripted_drive", "params": {"steps": [{"left": 0, "right": 0, "duration_s": 1}]}}]}
  if mesh:
    cfg["devices"] = {"pi": {}}
    cfg["mesh"] = {"transport": "udp"}
  (root / "robot.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
  (root / "fleet" / "rover-2.yaml").write_text("nodes: {scripted_drive: {params: {loop: false}}}\n", encoding="utf-8")
  (root / "fleet.yaml").write_text(
    "defaults: {remote_nervlynx: ~/.venv/bin/nervlynx, run_args: --control}\n"
    "robots:\n  rover-1: {host: pi@rover-1.local}\n  rover-2: {host: pi@rover-2.local, overlay: fleet/rover-2.yaml, port: 9200}\n",
    encoding="utf-8",
  )
  return root


def make_fleet(root: Path, robots: dict[str, FakeRobot], **kwargs) -> Fleet:
  clock = [0.0]
  lines: list[str] = []
  fleet = Fleet(
    root,
    load_fleet(root),
    shell=lambda robot: robots[robot.name],
    echo=lines.append,
    sleep=lambda s: clock.__setitem__(0, clock[0] + s),
    clock=lambda: clock[0],
    **kwargs,
  )
  fleet.lines = lines
  return fleet


def test_fleet_yaml_is_read_with_defaults_and_checked(tmp_path) -> None:
  root = project(tmp_path)
  robots = load_fleet(root)
  assert [r.name for r in robots] == ["rover-1", "rover-2"]
  assert robots[1].overlay == root / "fleet" / "rover-2.yaml" and robots[1].port == 9200 and robots[0].run_args == "--control"
  assert robots[0].pip_command == "~/.venv/bin/pip" and Robot("x", "h").pip_command == "pip3"
  assert [r.name for r in select(robots, "rover-2")] == ["rover-2"]
  with pytest.raises(RemoteError, match="no robot named rover-9"):
    select(robots, "rover-9")
  (root / "fleet.yaml").write_text("robots: {r: {host: pi@r, overlay: nope.yaml}}\n", encoding="utf-8")
  with pytest.raises(RemoteError, match="overlay .*nope.yaml not found"):
    load_fleet(root)
  (root / "fleet.yaml").write_text("robots: {r: {hots: pi@r}}\n", encoding="utf-8")
  with pytest.raises(RemoteError, match="unknown keys hots"):
    load_fleet(root)


def test_each_robot_gets_its_overlay_and_a_unique_mesh_name(tmp_path) -> None:
  root = project(tmp_path, mesh=True)
  robots = load_fleet(root)
  cfg = yaml.safe_load((root / "robot.yaml").read_text(encoding="utf-8"))
  one = yaml.safe_load(overlay_for(robots[0], cfg))
  two = yaml.safe_load(overlay_for(robots[1], cfg))
  assert one == {"mesh": {"robot": "rover-1"}}
  assert two == {"nodes": {"scripted_drive": {"params": {"loop": False}}}, "mesh": {"robot": "rover-2"}}
  assert overlay_for(robots[0], {"nodes": []}) is None


def test_deploy_updates_every_robot_and_keeps_the_last_release(tmp_path) -> None:
  root = project(tmp_path)
  robots = {"rover-1": FakeRobot("rover-1"), "rover-2": FakeRobot("rover-2", release=None)}
  results = make_fleet(root, robots).deploy()
  assert all(r["ok"] and r["result"] == "deployed" for r in results)
  one, two = robots["rover-1"], robots["rover-2"]
  assert one.log[0] == "snapshot old" and one.log[1] == "sync" and "write overlay.yaml" in one.log
  assert "overlay.yaml" not in one.files and "loop: false" in two.files["overlay.yaml"]
  new_id = json.loads(one.files[".nervlynx-release.json"])["id"]
  assert set(one.snapshots) == {"old", new_id} and one.restarts == 1
  assert json.loads(two.files[".nervlynx-release.json"])["robot"] == "rover-2"


def test_an_unhealthy_robot_goes_back_to_its_previous_release(tmp_path) -> None:
  root = project(tmp_path)
  robots = {
    "rover-1": FakeRobot("rover-1"),
    "rover-2": FakeRobot("rover-2", healthy=lambda files: "overlay.yaml" not in files),  # its overlay breaks it
  }
  fleet = make_fleet(root, robots)
  results = {r["robot"]: r for r in fleet.deploy()}
  assert results["rover-1"]["ok"] and not results["rover-2"]["ok"]
  assert results["rover-2"]["result"] == "unhealthy (fault); rolled back" and results["rover-2"]["release"] == "old"
  assert robots["rover-2"].release()["id"] == "old" and "overlay.yaml" not in robots["rover-2"].files
  assert any("unhealthy after restart" in line for line in fleet.lines)
  assert "rover-2  unhealthy (fault); rolled back" in render(results.values())


def test_a_release_that_fails_validation_is_never_started(tmp_path) -> None:
  root = project(tmp_path)
  robots = {"rover-1": FakeRobot("rover-1", valid=False), "rover-2": FakeRobot("rover-2", release=None, valid=False)}
  results = {r["robot"]: r for r in make_fleet(root, robots).deploy()}
  assert results["rover-1"]["result"] == "failed validation; rolled back" and robots["rover-1"].release()["id"] == "old"
  assert results["rover-2"]["result"] == "failed validation; there is no earlier release to go back to"
  assert robots["rover-2"].restarts == 0


def test_status_rollback_and_upgrade(tmp_path) -> None:
  root = project(tmp_path)
  robots = {"rover-1": FakeRobot("rover-1"), "rover-2": FakeRobot("rover-2")}
  fleet = make_fleet(root, robots)
  fleet.deploy()
  status = {r["robot"]: r for r in fleet.status()}
  assert status["rover-1"]["health"] == "ok" and status["rover-1"]["version"] == "0.2.0"
  back = fleet.rollback()
  assert all(r["release"] == "old" for r in back) and robots["rover-1"].release()["id"] == "old"
  missing = fleet.rollback("nope")
  assert all(not r["ok"] and "release nope is not on this robot" in r["detail"] for r in missing)
  upgraded = fleet.upgrade("nervlynx @ git+https://example/nervlynx@v0.3.0")
  assert all(r["ok"] for r in upgraded) and robots["rover-2"].upgraded == ["nervlynx @ git+https://example/nervlynx@v0.3.0"]


def test_latched_robots_count_as_healthy_only_without_a_failing_node() -> None:
  assert is_healthy({"service": "active", "health": "estop", "stale": [], "breakers": []})  # start_in_estop
  assert not is_healthy({"service": "active", "health": "estop", "stale": ["imu"], "breakers": []})
  assert not is_healthy({"service": "activating", "health": "ok"})


def test_ssh_shell_commands(tmp_path) -> None:
  calls: list = []

  def capture(argv):
    calls.append(list(argv))
    if argv[0] == "ssh" and argv[-1].startswith("python3 -c"):
      return 0, 'noise\nNLX{"service": "active", "health": "ok"}\n'
    return 0, ""

  shell = SshShell(Robot("rover-1", "pi@rover-1.local", remote_nervlynx="~/.venv/bin/nervlynx", port=9200), "rover", capture)
  shell.sync(tmp_path)
  rsync = calls[-1]
  assert rsync[0] == "rsync" and "--exclude=/calibration.yaml" in rsync and "--exclude=/fleet.yaml" in rsync
  shell.write("overlay.yaml", "mesh: {robot: rover-1}\n")
  assert calls[-1][-1] == "printf %s 'mesh: {robot: rover-1}\n' > ~/nervlynx-projects/rover/overlay.yaml"
  shell.restore("20260101-000000")
  assert "--exclude=/calibration.yaml" in calls[-1][-1] and "~/nervlynx-projects/.releases/rover/20260101-000000/" in calls[-1][-1]
  assert shell.probe() == {"service": "active", "health": "ok"}
  assert "127.0.0.1:%d/health\" % 9200" in calls[-1][-1] and "nervlynx-rover" in calls[-1][-1]


def test_fleet_cli(tmp_path, monkeypatch) -> None:
  root = project(tmp_path)
  robots = {"rover-1": FakeRobot("rover-1"), "rover-2": FakeRobot("rover-2", healthy=lambda files: False)}
  real = fleet_mod.Fleet

  def fake_fleet(project_dir, chosen, **kwargs):
    return real(project_dir, chosen, shell=lambda robot: robots[robot.name], sleep=lambda s: None, clock=iter(range(1000)).__next__, **kwargs)

  monkeypatch.setattr(fleet_mod, "Fleet", fake_fleet)
  runner = CliRunner()
  listed = runner.invoke(app, ["fleet", "list", "--project", str(root)])
  assert listed.exit_code == 0 and "rover-2          pi@rover-2.local  overlay fleet/rover-2.yaml" in listed.stdout
  deployed = runner.invoke(app, ["fleet", "deploy", "--project", str(root)])
  assert deployed.exit_code == 1 and "rolled back" in deployed.stdout
  one = runner.invoke(app, ["fleet", "status", "--project", str(root), "--robots", "rover-1"])
  assert one.exit_code == 0 and "rover-2" not in one.stdout.split("robot", 1)[1]
