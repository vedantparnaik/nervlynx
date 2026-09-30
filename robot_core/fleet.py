"""`nervlynx fleet`: deploy one project to many robots, check them, and roll back.

`fleet.yaml` beside robot.yaml lists the robots:

  defaults:
    remote_nervlynx: ~/.venv/bin/nervlynx
    run_args: --control
  robots:
    rover-1: {host: pi@rover-1.local}
    rover-2: {host: pi@rover-2.local, overlay: fleet/rover-2.yaml}   # its own pins, limits

`nervlynx fleet deploy` works on every robot in parallel. For each one it keeps the
running release as a snapshot on the robot, copies the project, writes that robot's
`overlay.yaml` (and a unique `mesh.robot` name, so robots sharing a network never hear
each other), checks it with `nervlynx validate`, restarts its service, and waits for the
dashboard's /health. If the new release fails validation or comes up unhealthy, the
robot goes back to its previous release on its own. Calibration and logs stay on each
robot. `status`, `rollback`, and `upgrade` (a new NervLynx version, over the air) work
the same way.
"""

from __future__ import annotations

import json
import shlex
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, Sequence

import yaml

from robot_core.overlay import OVERLAY_FILE
from robot_core.remote import REMOTE_ROOT, RSYNC_EXCLUDES, RemoteError, Target, _remote_path, _rsync_path, service_unit

FLEET_FILE = "fleet.yaml"
RELEASE_FILE = ".nervlynx-release.json"
KEEP_RELEASES = 5
SETTLE_S = 3.0
_ROBOT_KEYS = {"host", "overlay", "run_args", "remote_nervlynx", "port", "pip"}
_FLEET_EXCLUDES = (f"/{FLEET_FILE}", "/fleet/", f"/{RELEASE_FILE}")
Capture = Callable[[Sequence[str]], tuple[int, str]]


@dataclass(frozen=True)
class Robot:
  name: str
  host: str
  overlay: Path | None = None
  run_args: str = ""
  remote_nervlynx: str = "nervlynx"
  port: int = 9120
  pip: str | None = None

  def target(self, project: str) -> Target:
    return Target(self.host, project, f"{REMOTE_ROOT}/{project}", self.remote_nervlynx, self.run_args)

  @property
  def pip_command(self) -> str:
    if self.pip:
      return self.pip
    head, _, tail = self.remote_nervlynx.rpartition("/")
    return f"{head}/pip" if tail == "nervlynx" and head else "pip3"


def load_fleet(project_dir: Path) -> list[Robot]:
  path = project_dir / FLEET_FILE
  if not path.is_file():
    raise RemoteError(f"{path} not found; list your robots in it (see docs/FLEET.md)")
  raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
  if not isinstance(raw, dict) or not isinstance(raw.get("robots"), dict) or not raw["robots"]:
    raise RemoteError(f"{FLEET_FILE} needs robots: {{name: {{host: user@host}}, ...}}")
  defaults = raw.get("defaults") or {}
  if not isinstance(defaults, dict):
    raise RemoteError(f"{FLEET_FILE}: defaults must be a mapping")
  robots = []
  for name, spec in raw["robots"].items():
    spec = {**defaults, **(spec or {})}
    unknown = sorted(set(spec) - _ROBOT_KEYS)
    if unknown:
      raise RemoteError(f"{FLEET_FILE}: robot {name}: unknown keys {', '.join(unknown)}")
    host = spec.get("host")
    if not isinstance(host, str) or not host or host.startswith("-"):
      raise RemoteError(f"{FLEET_FILE}: robot {name} needs host: user@host")
    overlay = spec.get("overlay")
    overlay_path = project_dir / str(overlay) if overlay else None
    if overlay_path is not None and not overlay_path.is_file():
      raise RemoteError(f"{FLEET_FILE}: robot {name}: overlay {overlay_path} not found")
    robots.append(
      Robot(
        name=str(name),
        host=host,
        overlay=overlay_path,
        run_args=str(spec.get("run_args", "")),
        remote_nervlynx=str(spec.get("remote_nervlynx", "nervlynx")),
        port=int(spec.get("port", 9120)),
        pip=spec.get("pip"),
      )
    )
  return robots


def select(robots: list[Robot], names: str | None) -> list[Robot]:
  if not names:
    return robots
  wanted = [n.strip() for n in names.split(",") if n.strip()]
  known = {r.name: r for r in robots}
  missing = [n for n in wanted if n not in known]
  if missing:
    raise RemoteError(f"no robot named {', '.join(missing)} in {FLEET_FILE} (robots: {', '.join(known)})")
  return [known[n] for n in wanted]


class Shell(Protocol):
  """The operations fleet needs on one robot. `SshShell` does them over SSH."""

  def release(self) -> dict[str, Any] | None: ...
  def snapshot(self, release_id: str) -> None: ...
  def releases(self) -> list[str]: ...
  def sync(self, project_dir: Path) -> None: ...
  def write(self, name: str, text: str | None) -> None: ...
  def validate(self) -> tuple[bool, str]: ...
  def restart(self) -> None: ...
  def probe(self) -> dict[str, Any]: ...
  def restore(self, release_id: str) -> None: ...
  def prune(self, keep: int) -> None: ...
  def upgrade(self, spec: str) -> tuple[bool, str]: ...


def run_capture(argv: Sequence[str]) -> tuple[int, str]:
  try:
    done = subprocess.run(list(argv), capture_output=True, text=True, timeout=300)
  except subprocess.TimeoutExpired:
    return 124, f"{argv[0]} timed out"
  except OSError as exc:
    return 127, str(exc)
  return done.returncode, (done.stdout or "") + (done.stderr or "")


_PROBE = """
import json, os, subprocess, urllib.request
out = {}
try:
  out["service"] = subprocess.run(["systemctl", "--user", "is-active", SERVICE], capture_output=True, text=True, timeout=10).stdout.strip()
except Exception as exc:
  out["service"] = "unknown: %s" % exc
try:
  health = json.load(urllib.request.urlopen("http://127.0.0.1:%d/health" % PORT, timeout=3))
  out.update(health=health.get("status"), stale=health.get("stale_nodes"), breakers=health.get("open_breakers"))
except Exception as exc:
  out["health"] = None
  out["error"] = str(exc)
try:
  out["release"] = json.load(open(os.path.expanduser(RELEASE)))
except Exception:
  out["release"] = None
try:
  out["version"] = subprocess.run([os.path.expanduser(NERVLYNX), "version"], capture_output=True, text=True, timeout=20).stdout.strip()
except Exception:
  out["version"] = None
print("NLX" + json.dumps(out))
"""


class SshShell:
  """Fleet operations on one robot over SSH (ssh and rsync, as `nervlynx deploy` uses)."""

  def __init__(self, robot: Robot, project: str, capture: Capture = run_capture) -> None:
    self.robot = robot
    self.target = robot.target(project)
    self.capture = capture
    self.path = _remote_path(self.target.path)
    self.releases_dir = _remote_path(f"{REMOTE_ROOT}/.releases/{project}")

  def _ssh(self, command: str) -> tuple[int, str]:
    return self.capture(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.robot.host, command])

  def _ok(self, command: str, what: str) -> str:
    code, out = self._ssh(command)
    if code != 0:
      raise RemoteError(f"{what} failed (exit {code}): {out.strip()[-400:]}")
    return out

  def release(self) -> dict[str, Any] | None:
    code, out = self._ssh(f"cat {self.path}/{RELEASE_FILE} 2>/dev/null || true")
    try:
      data = json.loads(out.strip() or "null")
    except ValueError:
      return None
    return data if isinstance(data, dict) else None

  def snapshot(self, release_id: str) -> None:
    dest = f"{self.releases_dir}/{shlex.quote(release_id)}"
    self._ok(f"mkdir -p {dest} && rsync -a --delete --exclude=logs/ {self.path}/ {dest}/", "saving the running release")

  def releases(self) -> list[str]:
    code, out = self._ssh(f"ls -1 {self.releases_dir} 2>/dev/null || true")
    return sorted(line.strip() for line in out.splitlines() if line.strip())

  def sync(self, project_dir: Path) -> None:
    self._ok(f"mkdir -p {self.path}", "creating the project folder")
    excludes = [f"--exclude={pattern}" for pattern in RSYNC_EXCLUDES + _FLEET_EXCLUDES]
    code, out = self.capture(["rsync", "-az", "--delete", *excludes, f"{project_dir.resolve()}/", f"{self.robot.host}:{_rsync_path(self.target.path)}/"])
    if code != 0:
      raise RemoteError(f"copying the project failed (exit {code}): {out.strip()[-400:]}")

  def write(self, name: str, text: str | None) -> None:
    path = f"{self.path}/{shlex.quote(name)}"
    if text is None:
      self._ok(f"rm -f {path}", f"removing {name}")
    else:
      self._ok(f"printf %s {shlex.quote(text)} > {path}", f"writing {name}")

  def validate(self) -> tuple[bool, str]:
    code, out = self._ssh(f"cd {self.path} && {self.target.nervlynx} validate robot.yaml")
    return code == 0, out.strip()

  def restart(self) -> None:
    unit = shlex.quote(service_unit(self.target))
    service = self.target.service
    self._ok(
      f"mkdir -p ~/.config/systemd/user && printf %s {unit} > ~/.config/systemd/user/{service}.service"
      f" && systemctl --user daemon-reload && systemctl --user enable {service} && systemctl --user restart {service}",
      f"restarting {service}",
    )

  def probe(self) -> dict[str, Any]:
    script = _PROBE.replace("SERVICE", repr(self.target.service)).replace("PORT", str(self.robot.port))
    script = script.replace("RELEASE", repr(f"{self.target.path}/{RELEASE_FILE}")).replace("NERVLYNX", repr(self.target.nervlynx))
    code, out = self._ssh(f"python3 -c {shlex.quote(script)}")
    for line in out.splitlines():
      if line.startswith("NLX"):
        return json.loads(line[3:])
    return {"service": None, "health": None, "error": out.strip()[-200:] or f"ssh exit {code}"}

  def restore(self, release_id: str) -> None:
    source = f"{self.releases_dir}/{shlex.quote(release_id)}"
    self._ok(
      f"test -d {source} && rsync -a --delete --exclude=logs/ --exclude=/calibration.yaml {source}/ {self.path}/",
      f"restoring release {release_id}",
    )

  def prune(self, keep: int) -> None:
    self._ssh(f"cd {self.releases_dir} 2>/dev/null && ls -1 | sort | head -n -{int(keep)} | xargs -r rm -rf")

  def upgrade(self, spec: str) -> tuple[bool, str]:
    code, out = self._ssh(f"{self.robot.pip_command} install --upgrade {shlex.quote(spec)}")
    return code == 0, out.strip()[-400:]


def is_healthy(state: dict[str, Any]) -> bool:
  """Running, and either fine or latched without a failing node (e.g. start_in_estop)."""
  if state.get("service") != "active":
    return False
  health = state.get("health")
  if health in ("ok", "degraded"):
    return True
  return health == "estop" and not state.get("stale") and not state.get("breakers")


def release_id(now: float | None = None) -> str:
  return time.strftime("%Y%m%d-%H%M%S", time.gmtime(now if now is not None else time.time()))


def _git_commit(project_dir: Path) -> str | None:
  code, out = run_capture(["git", "-C", str(project_dir), "rev-parse", "--short", "HEAD"])
  return out.strip() if code == 0 else None


def overlay_for(robot: Robot, cfg: dict[str, Any]) -> str | None:
  """The overlay.yaml a robot gets: its own file, plus a unique mesh identity."""
  data: dict[str, Any] = {}
  if robot.overlay is not None:
    loaded = yaml.safe_load(robot.overlay.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
      raise RemoteError(f"{robot.overlay} must be a YAML mapping")
    data = loaded
  if isinstance(cfg.get("mesh"), dict):
    mesh = data.setdefault("mesh", {})
    if isinstance(mesh, dict):
      mesh.setdefault("robot", robot.name)
  if not data:
    return None
  return f"# Written by nervlynx fleet deploy for {robot.name}.\n" + yaml.safe_dump(data, sort_keys=False)


class Fleet:
  """Runs one fleet operation on every selected robot in parallel."""

  def __init__(
    self,
    project_dir: Path,
    robots: list[Robot],
    *,
    shell: Callable[[Robot], Shell] | None = None,
    echo: Callable[[str], Any] = print,
    parallel: int = 4,
    health_timeout_s: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
  ) -> None:
    """A new release must stay healthy for SETTLE_S within `health_timeout_s` of its restart."""
    self.project_dir = project_dir
    self.project = project_dir.resolve().name
    self.robots = robots
    self.shell = shell or (lambda robot: SshShell(robot, self.project))
    self.echo = echo
    self.parallel = max(1, int(parallel))
    self.health_timeout_s = float(health_timeout_s)
    self.sleep = sleep
    self.clock = clock

  def _each(self, work: Callable[[Robot], dict[str, Any]]) -> list[dict[str, Any]]:
    def safe(robot: Robot) -> dict[str, Any]:
      try:
        return work(robot)
      except RemoteError as exc:
        self.echo(f"[{robot.name}] {exc}")
        return {"robot": robot.name, "ok": False, "result": "failed", "detail": str(exc)}

    with ThreadPoolExecutor(max_workers=min(self.parallel, len(self.robots) or 1)) as pool:
      return list(pool.map(safe, self.robots))

  def _healthy(self, robot: Robot, shell: Shell) -> tuple[bool, dict[str, Any]]:
    """Healthy only if it stays healthy for SETTLE_S: a sensor that fails a second after
    start-up should still trigger a rollback."""
    deadline = self.clock() + self.health_timeout_s
    healthy_since: float | None = None
    while True:
      state = shell.probe()
      now = self.clock()
      if is_healthy(state):
        healthy_since = now if healthy_since is None else healthy_since
        if now - healthy_since >= SETTLE_S:
          return True, state
      else:
        healthy_since = None
        if state.get("service") == "failed" or now >= deadline:
          return False, state
      self.sleep(1.0)

  def deploy(self, *, rollback: bool = True) -> list[dict[str, Any]]:
    cfg = yaml.safe_load((self.project_dir / "robot.yaml").read_text(encoding="utf-8")) or {}
    stamp = release_id()
    meta = {"id": stamp, "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "from": socket.gethostname(), "git": _git_commit(self.project_dir)}

    def work(robot: Robot) -> dict[str, Any]:
      shell = self.shell(robot)
      say = lambda text: self.echo(f"[{robot.name}] {text}")  # noqa: E731
      previous = shell.release()
      if previous and previous.get("id"):
        shell.snapshot(str(previous["id"]))
        say(f"saved release {previous['id']}")
      shell.sync(self.project_dir)
      shell.write(OVERLAY_FILE, overlay_for(robot, cfg))
      shell.write(RELEASE_FILE, json.dumps({**meta, "robot": robot.name}))
      ok, output = shell.validate()
      if not ok:
        say(f"robot.yaml does not validate there: {output[-300:]}")
        return self._undo(robot, shell, previous, rollback, "failed validation")
      shell.restart()
      healthy, state = self._healthy(robot, shell)
      if not healthy:
        say(f"unhealthy after restart: service {state.get('service')}, health {state.get('health')} {state.get('error', '')}".rstrip())
        return self._undo(robot, shell, previous, rollback, f"unhealthy ({state.get('health') or state.get('service')})")
      shell.snapshot(stamp)
      shell.prune(KEEP_RELEASES)
      say(f"deployed {stamp}: {state.get('health')}")
      return {"robot": robot.name, "ok": True, "result": "deployed", "release": stamp, "health": state.get("health")}

    return self._each(work)

  def _undo(self, robot: Robot, shell: Shell, previous: dict[str, Any] | None, rollback: bool, why: str) -> dict[str, Any]:
    if not rollback or not previous or not previous.get("id"):
      reason = "rollback is off" if not rollback else "there is no earlier release to go back to"
      return {"robot": robot.name, "ok": False, "result": f"{why}; {reason}", "release": None, "health": None}
    shell.restore(str(previous["id"]))
    shell.restart()
    healthy, state = self._healthy(robot, shell)
    self.echo(f"[{robot.name}] rolled back to {previous['id']}: {state.get('health')}")
    return {"robot": robot.name, "ok": False, "result": f"{why}; rolled back", "release": previous["id"], "health": state.get("health") if healthy else "unhealthy"}

  def rollback(self, to: str | None = None) -> list[dict[str, Any]]:
    def work(robot: Robot) -> dict[str, Any]:
      shell = self.shell(robot)
      current = (shell.release() or {}).get("id")
      choices = [r for r in shell.releases() if r != current]
      target = to or (choices[-1] if choices else None)
      if target is None:
        raise RemoteError("no earlier release on this robot")
      if to is not None and to not in shell.releases():
        raise RemoteError(f"release {to} is not on this robot (it has: {', '.join(shell.releases()) or 'none'})")
      shell.restore(target)
      shell.restart()
      healthy, state = self._healthy(robot, shell)
      self.echo(f"[{robot.name}] now on {target}: {state.get('health')}")
      return {"robot": robot.name, "ok": healthy, "result": "rolled back", "release": target, "health": state.get("health")}

    return self._each(work)

  def status(self) -> list[dict[str, Any]]:
    def work(robot: Robot) -> dict[str, Any]:
      state = self.shell(robot).probe()
      release = state.get("release") or {}
      healthy = is_healthy(state)
      return {
        "robot": robot.name,
        "ok": healthy,
        "result": state.get("service") or "unreachable",
        "health": state.get("health") or ("unreachable" if state.get("service") is None else "no dashboard"),
        "release": release.get("id"),
        "version": state.get("version"),
      }

    return self._each(work)

  def upgrade(self, spec: str) -> list[dict[str, Any]]:
    def work(robot: Robot) -> dict[str, Any]:
      shell = self.shell(robot)
      ok, output = shell.upgrade(spec)
      if not ok:
        raise RemoteError(f"upgrading NervLynx failed: {output}")
      shell.restart()
      healthy, state = self._healthy(robot, shell)
      self.echo(f"[{robot.name}] NervLynx {state.get('version')}: {state.get('health')}")
      return {"robot": robot.name, "ok": healthy, "result": "upgraded", "health": state.get("health"), "version": state.get("version")}

    return self._each(work)


def render(results: Iterable[dict[str, Any]]) -> str:
  rows = [("robot", "result", "health", "release", "version")]
  for r in results:
    rows.append((r["robot"], str(r.get("result", "")), str(r.get("health") or "-"), str(r.get("release") or "-"), str(r.get("version") or "-")))
  widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
  keep = [i for i in range(len(widths)) if any(row[i] not in ("-", "") for row in rows[1:])] or [0]
  return "\n".join("  ".join(row[i].ljust(widths[i]) for i in keep).rstrip() for row in rows)
