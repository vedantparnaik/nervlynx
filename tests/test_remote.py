import shlex
from pathlib import Path

import pytest

from robot_core.remote import RemoteError, deploy, logs, pull, service_unit, target_for


class Recorder:
  def __init__(self, fail_at: int | None = None):
    self.calls: list[list[str]] = []
    self.fail_at = fail_at

  def __call__(self, argv):
    self.calls.append(list(argv))
    return 1 if self.fail_at is not None and len(self.calls) - 1 == self.fail_at else 0


def project(tmp_path: Path, name: str = "my-rover") -> Path:
  root = tmp_path / name
  root.mkdir()
  (root / "robot.yaml").write_text("name: x\nnodes: []\n", encoding="utf-8")
  return root


def test_deploy_syncs_validates_and_installs_the_service(tmp_path: Path) -> None:
  root = project(tmp_path)
  target = target_for("pi@my-rover.local", root, run_args="--control")
  runner, lines = Recorder(), []
  assert deploy(target, root, install_service=True, runner=runner, echo=lines.append) == 0
  mkdir, rsync, validate, install, restart = runner.calls
  assert mkdir == ["ssh", "pi@my-rover.local", "mkdir -p ~/nervlynx-projects/my-rover"]
  assert rsync[:3] == ["rsync", "-az", "--delete"] and "--exclude=logs/" in rsync and "--exclude=.venv/" in rsync
  assert rsync[-2:] == [f"{root.resolve()}/", "pi@my-rover.local:nervlynx-projects/my-rover/"]
  assert validate == ["ssh", "pi@my-rover.local", "cd ~/nervlynx-projects/my-rover && nervlynx validate robot.yaml"]
  spaced = target_for("pi@my-rover.local", root, path="~/my robots/rover")
  deploy(spaced, root, validate=False, runner=(spaced_runner := Recorder()), echo=lambda line: None)
  assert spaced_runner.calls[0][2] == "mkdir -p ~/'my robots/rover'"
  assert "systemctl --user enable nervlynx-my-rover" in install[2]
  assert restart == ["ssh", "pi@my-rover.local", "systemctl --user restart nervlynx-my-rover"]
  assert lines[-1].startswith("it starts at boot once lingering is on")


def test_service_unit_survives_shell_quoting() -> None:
  target = target_for("pi@robot", Path("/tmp/it's-mine"), run_args="--control")
  unit = service_unit(target)
  assert "WorkingDirectory=%h/nervlynx-projects/it's-mine" in unit
  assert "ExecStart=/bin/sh -lc 'exec nervlynx run robot.yaml --quiet --control'" in unit
  assert "KillSignal=SIGINT" in unit
  assert shlex.split(f"printf %s {shlex.quote(unit)}")[2] == unit


def test_deploy_stops_at_the_first_failing_step_with_a_hint(tmp_path: Path) -> None:
  root = project(tmp_path)
  runner, lines = Recorder(fail_at=2), []
  assert deploy(target_for("robot", root), root, runner=runner, echo=lines.append) == 1
  assert len(runner.calls) == 3
  assert "is NervLynx installed on the robot?" in lines[-1]


def test_deploy_needs_a_project_and_a_host(tmp_path: Path) -> None:
  with pytest.raises(RemoteError, match="has no robot.yaml"):
    deploy(target_for("robot", tmp_path), tmp_path, runner=Recorder())
  with pytest.raises(RemoteError, match="user@host"):
    target_for("-oProxyCommand=evil", tmp_path)


def test_logs_and_pull_commands(tmp_path: Path) -> None:
  root = project(tmp_path)
  target = target_for("pi@robot", root, path="/srv/robots/my-rover")
  runner = Recorder()
  logs(target, follow=True, lines=20, runner=runner)
  assert runner.calls[-1] == ["ssh", "-t", "pi@robot", "journalctl --user -u nervlynx-my-rover -n 20 -f"]
  pull(target, tmp_path / "runs", runner=runner)
  assert runner.calls[-1] == ["rsync", "-az", "pi@robot:/srv/robots/my-rover/logs/live/", f"{(tmp_path / 'runs').resolve()}/"]
