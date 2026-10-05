"""`nervlynx deploy`, `logs`, and `pull`: work on the robot from your laptop over SSH.

The project folder (robot.yaml, nodes/, anything else) is copied with rsync to
`~/nervlynx-projects/<name>` on the robot, validated there, and optionally run as a
systemd user service (`nervlynx-<name>`) that starts at boot. Runs recorded on the robot
come back with `pull` so they can be inspected and replayed on the laptop.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

Runner = Callable[[Sequence[str]], int]
# calibration.yaml and overlay.yaml belong to one robot: never sent, never deleted there.
RSYNC_EXCLUDES = ("logs/", "__pycache__/", "*.pyc", ".git/", ".venv/", "/calibration.yaml", "/overlay.yaml")
REMOTE_ROOT = "~/nervlynx-projects"


class RemoteError(RuntimeError):
  pass


def run_command(argv: Sequence[str]) -> int:
  return subprocess.call(list(argv))


@dataclass(frozen=True)
class Target:
  host: str
  project: str
  path: str
  nervlynx: str = "nervlynx"
  run_args: str = ""

  @property
  def service(self) -> str:
    return f"nervlynx-{self.project}"


def target_for(host: str, project_dir: Path, *, path: str | None = None, nervlynx: str = "nervlynx", run_args: str = "") -> Target:
  if not host or host.startswith("-"):
    raise RemoteError("give the robot as user@host or an ssh config name, e.g. pi@my-rover.local")
  name = project_dir.resolve().name
  return Target(host, name, path or f"{REMOTE_ROOT}/{name}", nervlynx, run_args)


def _remote_path(path: str) -> str:
  """Quote a remote path for ssh commands, keeping a leading ~/ so the remote shell expands it."""
  if path.startswith("~/"):
    return "~/" + shlex.quote(path[2:])
  return shlex.quote(path)


def _rsync_path(path: str) -> str:
  """rsync resolves relative remote paths against the home directory, whatever its version
  does with `~`, so drop the ~/ prefix instead of relying on the remote shell."""
  return path[2:] if path.startswith("~/") else path


def _need(tool: str) -> None:
  if shutil.which(tool) is None:
    raise RemoteError(f"{tool} is not installed on this computer")


def service_unit(target: Target) -> str:
  return (
    "[Unit]\n"
    f"Description=NervLynx robot {target.project}\n"
    "After=network-online.target\n\n"
    "[Service]\n"
    f"WorkingDirectory=%h/{target.path[2:] if target.path.startswith('~/') else target.path}\n"
    f"ExecStart=/bin/sh -lc 'exec {target.nervlynx} run robot.yaml --quiet{' ' + target.run_args if target.run_args else ''}'\n"
    "KillSignal=SIGINT\n"
    "TimeoutStopSec=10\n"
    "Restart=on-failure\n"
    "RestartSec=3\n"
    "# Lets NervLynx arm a watchdog once it runs; systemd restarts it if it then freezes.\n"
    "NotifyAccess=main\n"
    "TimeoutAbortSec=5\n\n"
    "[Install]\n"
    "WantedBy=default.target\n"
  )


def deploy(
  target: Target,
  project_dir: Path,
  *,
  install_service: bool = False,
  restart: bool = False,
  validate: bool = True,
  runner: Runner = run_command,
  echo: Callable[[str], object] = print,
) -> int:
  """Copy the project, validate it on the robot, and optionally (re)start its service."""
  if not (project_dir / "robot.yaml").exists():
    raise RemoteError(f"{project_dir} has no robot.yaml; run deploy from a project folder (see nervlynx new)")
  if runner is run_command:
    _need("ssh")
    _need("rsync")
  remote = _remote_path(target.path)
  steps: list[tuple[str, list[str]]] = [
    ("create the project folder", ["ssh", target.host, f"mkdir -p {remote}"]),
    (
      "copy the project",
      [
        "rsync",
        "-az",
        "--delete",
        *[f"--exclude={pattern}" for pattern in RSYNC_EXCLUDES],
        f"{project_dir.resolve()}/",
        f"{target.host}:{_rsync_path(target.path)}/",
      ],
    ),
  ]
  if validate:
    steps.append(("validate it on the robot", ["ssh", target.host, f"cd {remote} && {target.nervlynx} validate robot.yaml"]))
  if install_service:
    unit = shlex.quote(service_unit(target))
    steps.append(
      (
        f"install the {target.service} service",
        [
          "ssh",
          target.host,
          f"mkdir -p ~/.config/systemd/user && printf %s {unit} > ~/.config/systemd/user/{target.service}.service"
          f" && systemctl --user daemon-reload && systemctl --user enable {target.service}",
        ],
      )
    )
  if install_service or restart:
    steps.append((f"restart {target.service}", ["ssh", target.host, f"systemctl --user restart {target.service}"]))
  for label, argv in steps:
    echo(f"-> {label}")
    code = runner(argv)
    if code != 0:
      hint = ""
      if label.startswith("validate"):
        hint = " (is NervLynx installed on the robot? pass --remote-nervlynx /path/to/nervlynx if it is not on PATH)"
      echo(f"deploy_failed step={label!r} exit={code}{hint}")
      return code
  echo(f"deployed {target.project} to {target.host}:{target.path}")
  if install_service:
    echo(f"it starts at boot once lingering is on: ssh {target.host} sudo loginctl enable-linger $USER")
  return 0


def logs(target: Target, *, follow: bool = True, lines: int = 100, runner: Runner = run_command) -> int:
  if runner is run_command:
    _need("ssh")
  cmd = f"journalctl --user -u {target.service} -n {int(lines)}" + (" -f" if follow else "")
  return runner(["ssh", "-t", target.host, cmd] if follow else ["ssh", target.host, cmd])


def pull(target: Target, destination: Path, *, runner: Runner = run_command) -> int:
  """Copy the robot's recorded runs (logs/live/) into `destination`."""
  if runner is run_command:
    _need("rsync")
  destination.mkdir(parents=True, exist_ok=True)
  return runner(["rsync", "-az", f"{target.host}:{_rsync_path(target.path)}/logs/live/", f"{destination.resolve()}/"])
