from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="NervLynx: create, simulate, check, and run robot projects.")
DEFAULT_CONFIG = Path("robot.yaml")


def _require_config(config: Path) -> None:
  if not config.exists():
    hint = " Create a project with: nervlynx new my-robot" if config == DEFAULT_CONFIG else ""
    typer.echo(f"{config} not found.{hint}")
    raise typer.Exit(code=1)


@app.command("new")
def new_project(
  name: Optional[str] = typer.Argument(None, help="Project folder name, e.g. my-rover."),
  template: str = typer.Option("obstacle-avoider", "--template", "-t", help="Template to start from (see --list)."),
  output_dir: Path = typer.Option(Path("."), "--dir", help="Folder to create the project in."),
  force: bool = typer.Option(False, "--force", help="Overwrite files in an existing folder."),
  list_templates: bool = typer.Option(False, "--list", help="List the available templates."),
) -> None:
  """Create a robot project that runs in the simulator and on a Raspberry Pi."""
  from robot_core.scaffold import TEMPLATES, ScaffoldError, create_project

  if list_templates or name is None:
    typer.echo("templates:")
    for item in TEMPLATES.values():
      typer.echo(f"  {item.name:<18} {item.summary}")
    if name is None and not list_templates:
      typer.echo("\nusage: nervlynx new <name> [--template NAME]")
      raise typer.Exit(code=2)
    return
  try:
    create_project(name, template, output_dir, force=force)
  except ScaffoldError as exc:
    typer.echo(f"new_error: {exc}")
    raise typer.Exit(code=1)
  root = output_dir / name
  typer.echo(f"Created {root} from the {template} template.\n")
  typer.echo("Next:")
  typer.echo(f"  cd {root}")
  typer.echo("  nervlynx sim         # try it in the simulator: http://127.0.0.1:9120/")
  typer.echo("  nervlynx validate    # check robot.yaml for both sim and robot")
  typer.echo("  nervlynx run         # on the Raspberry Pi (see README.md for wiring)")


@app.command("validate")
def validate(config: Path = typer.Argument(DEFAULT_CONFIG, help="Robot config to check.")) -> None:
  """Check a robot config (and its nodes/*.py) for both simulation and the robot."""
  from robot_core.live_config import MODES, uses_modes, validate_live_config
  from robot_core.overlay import overlay_paths
  from robot_core.project import load_project

  _require_config(config)
  try:
    cfg, registry, problems = load_project(config)
  except Exception as exc:  # noqa: BLE001 - report any load failure the same way
    typer.echo(f"{config}: can't be read: {exc}")
    raise typer.Exit(code=1)
  issues = list(problems)
  modes = MODES if uses_modes(cfg) else (None,)
  for mode in modes:
    label = f"[{mode}] " if mode else ""
    issues += [label + issue for issue in validate_live_config(cfg, registry, mode=mode)]
  if issues:
    for issue in issues:
      typer.echo(f"{config}: {issue}")
    typer.echo(f"{len(issues)} problem{'s' if len(issues) != 1 else ''} found")
    raise typer.Exit(code=1)
  where = "sim and robot modes" if uses_modes(cfg) else "every mode"
  overlays = overlay_paths(config)
  with_overlays = f", with {' and '.join(path.name for path in overlays)}" if overlays else ""
  typer.echo(f"{config}: ok in {where} ({len(cfg['nodes'])} nodes{with_overlays})")


@app.command("sim")
def sim(
  config: Path = typer.Argument(DEFAULT_CONFIG, help="Robot config to simulate."),
  duration_s: Optional[float] = typer.Option(None, "--duration-s", help="Stop after this long (default: until Ctrl-C, or 60 s with --fast)."),
  fast: bool = typer.Option(False, "--fast", help="Simulated clock: as fast as the CPU allows and repeatable; no dashboard."),
  host: str = typer.Option("127.0.0.1", "--host", help="Dashboard address (0.0.0.0 to reach it from another device)."),
  port: int = typer.Option(9120, "--port"),
  no_server: bool = typer.Option(False, "--no-server", help="Do not start the dashboard."),
  strict: bool = typer.Option(False, "--strict", help="Exit 2 on node errors, watchdog faults, e-stops, or collisions."),
  quiet: bool = typer.Option(False, "--quiet", help="Do not print the report at the end."),
  run_dir: Optional[Path] = typer.Option(None, "--run-dir", help="Where to write the run report and trace."),
  device: Optional[str] = typer.Option(None, "--device", help="Simulate only this device's nodes and join the mesh (default: every node here)."),
) -> None:
  """Run the project in simulation: mock pins, `only: sim` nodes, driving allowed from the dashboard."""
  from robot_core.session import SessionOptions, run_session

  _require_config(config)
  opts = SessionOptions(
    device=device,
    mode="sim",
    duration_s=duration_s if duration_s is not None or not fast else 60.0,
    sim_time=fast,
    host=host,
    port=port,
    no_server=no_server,
    allow_control=True,
    run_dir=run_dir,
    strict=strict,
    quiet=quiet,
    fail_on_collision=True,
  )
  raise typer.Exit(code=run_session(config, opts, typer.echo))


@app.command("run")
def run(
  config: Path = typer.Argument(DEFAULT_CONFIG, help="Robot config to run."),
  duration_s: Optional[float] = typer.Option(None, "--duration-s", help="Stop after this long (default: until Ctrl-C)."),
  control: bool = typer.Option(False, "--control", help="Allow driving and clearing the e-stop from the dashboard."),
  control_token: Optional[str] = typer.Option(None, "--control-token", envvar="NERVLYNX_CONTROL_TOKEN", help="Require this token for dashboard control."),
  host: str = typer.Option("0.0.0.0", "--host", help="Dashboard address (default: reachable from other devices)."),
  port: int = typer.Option(9120, "--port"),
  no_server: bool = typer.Option(False, "--no-server", help="Do not start the dashboard."),
  quiet: bool = typer.Option(False, "--quiet", help="Do not print the report at the end."),
  run_dir: Optional[Path] = typer.Option(None, "--run-dir", help="Where to write the run report and trace."),
  device: Optional[str] = typer.Option(None, "--device", help="Which of the config's devices this is (default: matched by hostname)."),
) -> None:
  """Run the project on the robot: real pins (backend auto), `only: robot` nodes."""
  import platform

  from robot_core.session import SessionOptions, run_session

  _require_config(config)
  if not no_server and host in ("0.0.0.0", ""):
    typer.echo(f"open http://{platform.node() or 'localhost'}.local:{port}/ from a phone or laptop on the same network")
  opts = SessionOptions(
    device=device,
    mode="robot",
    duration_s=duration_s,
    host=host,
    port=port,
    no_server=no_server,
    allow_control=control,
    control_token=control_token,
    run_dir=run_dir,
    quiet=quiet,
  )
  raise typer.Exit(code=run_session(config, opts, typer.echo))


def _write(path: Path, content: str) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(content, encoding="utf-8")


@app.command("init")
def init_project(
  project_name: str = typer.Argument(..., help="Plugin-pack project directory name."),
  output_dir: Path = typer.Option(Path("."), help="Parent directory where scaffold is created."),
  force: bool = typer.Option(False, "--force", help="Overwrite files if target already exists."),
) -> None:
  """Scaffold a starter NervLynx plugin-pack project."""
  pkg = project_name.replace("-", "_")
  root = output_dir / project_name
  if root.exists() and any(root.iterdir()) and not force:
    typer.echo(f"target directory is not empty: {root}. Use --force to overwrite.")
    raise typer.Exit(code=1)

  _write(
    root / "README.md",
    f"""# {project_name}

Starter NervLynx plugin pack generated by `nervlynx init`.

## Install

```bash
pip install -e .
```

## Validate

```bash
pytest -q
robot-core plugin-catalog
```
""",
  )

  _write(
    root / "pyproject.toml",
    f"""[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "{project_name}"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["nervlynx"]

[project.optional-dependencies]
dev = ["pytest>=8.0.0"]

[project.entry-points."nervlynx.sensors"]
{pkg}_camera_sensor = "{pkg}.plugins:CameraIngestSensorPlugin"

[project.entry-points."nervlynx.nodes"]
{pkg}_planner = "{pkg}.plugins:PlannerStubNodePlugin"
{pkg}_actuator = "{pkg}.plugins:ActuatorMockNodePlugin"

[tool.setuptools]
package-dir = {{"": "."}}

[tool.setuptools.packages.find]
where = ["."]
include = ["{pkg}*"]
""",
  )

  _write(root / pkg / "__init__.py", "__all__ = []\n")

  _write(
    root / pkg / "plugins.py",
    """from __future__ import annotations

from dataclasses import dataclass

from robot_core.plugins import NodePlugin, SensorPlugin
from robot_core.runtime import RuntimeMessage


@dataclass(frozen=True)
class CameraIngestSensorPlugin(SensorPlugin):
  name: str = "starter_camera_sensor"

  def read(self) -> dict[str, object]:
    return {"camera_count": 2, "gps_fix": True, "imu_ok": True, "ai_ok": True}


@dataclass(frozen=True)
class PlannerStubNodePlugin(NodePlugin):
  name: str = "starter_planner"
  input_topics: list[str] = None  # type: ignore[assignment]

  def __post_init__(self) -> None:
    object.__setattr__(self, "input_topics", ["perception.scene"])

  def handle(self, msg: RuntimeMessage) -> list[tuple[str, str, dict[str, object]]]:
    confidence = float(msg.payload.get("confidence", 0.0))
    mode = "go" if confidence >= 0.7 else "safe_slow"
    return [("mission.command", "MissionCommand", {"mode": mode, "confidence": confidence})]


@dataclass(frozen=True)
class ActuatorMockNodePlugin(NodePlugin):
  name: str = "starter_actuator"
  input_topics: list[str] = None  # type: ignore[assignment]

  def __post_init__(self) -> None:
    object.__setattr__(self, "input_topics", ["mission.command"])

  def handle(self, msg: RuntimeMessage) -> list[tuple[str, str, dict[str, object]]]:
    return [("actuator.feedback", "ActuatorFeedback", {"ok": True, "mode": msg.payload.get("mode", "unknown")})]
""",
  )

  _write(
    root / "config" / "graph.yaml",
    f"""name: {project_name}_graph
seed_topic: sensors.bundle
seed_schema: SensorBundle
seed_payload:
  camera_count: 4
  gps_fix: true
  imu_ok: true
  ai_ok: true
nodes:
  - plugin: {pkg}_planner
    input_topics: ["perception.scene"]
  - plugin: {pkg}_actuator
    input_topics: ["mission.command"]
""",
  )

  _write(
    root / "tests" / "test_plugins.py",
    f"""from {pkg}.plugins import ActuatorMockNodePlugin, CameraIngestSensorPlugin, PlannerStubNodePlugin
from robot_core.runtime import Envelope, RuntimeMessage


def test_sensor_returns_normalized_bundle() -> None:
  payload = CameraIngestSensorPlugin().read()
  assert "camera_count" in payload
  assert "gps_fix" in payload


def test_nodes_produce_expected_topics() -> None:
  planner = PlannerStubNodePlugin()
  actuator = ActuatorMockNodePlugin()
  msg = RuntimeMessage(
    envelope=Envelope(
      topic="perception.scene",
      source="perception",
      sequence=1,
      monotonic_time_ns=1,
      trace_id="starter",
      schema="SceneState",
    ),
    payload={{"confidence": 0.9}},
  )
  command = planner.handle(msg)[0]
  assert command[0] == "mission.command"
  feedback_input = RuntimeMessage(
    envelope=Envelope(
      topic="mission.command",
      source="planner",
      sequence=1,
      monotonic_time_ns=2,
      trace_id="starter",
      schema="MissionCommand",
    ),
    payload=command[2],
  )
  assert actuator.handle(feedback_input)[0][0] == "actuator.feedback"
""",
  )

  typer.echo(f"scaffold_created path={root}")


@app.command("doctor")
def doctor(
  config: Optional[Path] = typer.Argument(None, help="Robot config (live graph YAML) to validate as well."),
  as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
  """Check this machine for the setup problems that stop robots working."""
  from robot_core.doctor import FAIL, render_text, run_checks

  checks = run_checks(config=config)
  if as_json:
    typer.echo(json.dumps([check.to_dict() for check in checks], indent=2))
  else:
    typer.echo(render_text(checks))
  raise typer.Exit(code=1 if any(check.status == FAIL for check in checks) else 0)


def _target(host: str, project: Path, path: Optional[str], remote_nervlynx: str, run_args: str = ""):
  from robot_core.remote import RemoteError, target_for

  try:
    return target_for(host, project, path=path, nervlynx=remote_nervlynx, run_args=run_args)
  except RemoteError as exc:
    typer.echo(f"remote_error: {exc}")
    raise typer.Exit(code=2)


def _device_host(project: Path, device: str) -> str | None:
  import yaml

  try:
    cfg = yaml.safe_load((project / "robot.yaml").read_text(encoding="utf-8")) or {}
  except (OSError, yaml.YAMLError):
    return None
  devices = cfg.get("devices") if isinstance(cfg, dict) else None
  if not isinstance(devices, dict) or device not in devices:
    typer.echo(f"remote_error: robot.yaml has no device named {device!r}" + (f" (devices: {', '.join(devices)})" if isinstance(devices, dict) else ""))
    raise typer.Exit(code=2)
  host = (devices[device] or {}).get("host")
  return str(host) if host else None


@app.command("deploy")
def deploy_cmd(
  host: Optional[str] = typer.Argument(None, help="The robot: user@host or an ssh config name, e.g. pi@my-rover.local (default: the device's host)."),
  project: Path = typer.Option(Path("."), "--project", help="Project folder to copy (the one with robot.yaml)."),
  path: Optional[str] = typer.Option(None, "--path", help="Folder on the robot (default: ~/nervlynx-projects/<project name>)."),
  service: bool = typer.Option(False, "--service", help="Install a systemd user service that runs the project at boot, and start it."),
  restart: bool = typer.Option(False, "--restart", help="Restart the already-installed service after copying."),
  run_args: str = typer.Option("", "--run-args", help="Extra `nervlynx run` options for the service, e.g. \"--control\"."),
  no_validate: bool = typer.Option(False, "--no-validate", help="Skip `nervlynx validate` on the robot."),
  remote_nervlynx: str = typer.Option("nervlynx", "--remote-nervlynx", help="nervlynx command on the robot, e.g. ~/.venv/bin/nervlynx."),
  device: Optional[str] = typer.Option(None, "--device", help="Deploy to this device from robot.yaml's devices: (its host, and a service that runs its nodes)."),
) -> None:
  """Copy this project to the robot over SSH, check it there, and optionally run it as a service."""
  import shlex

  from robot_core.remote import RemoteError, deploy

  if device is not None:
    host = host or _device_host(project, device)
    run_args = f"--device {shlex.quote(device)}" + (f" {run_args}" if run_args else "")
  if not host:
    typer.echo("remote_error: give the robot as user@host" + (f", or set devices.{device}.host in robot.yaml" if device else ""))
    raise typer.Exit(code=2)
  target = _target(host, project, path, remote_nervlynx, run_args)
  try:
    code = deploy(target, project, install_service=service, restart=restart, validate=not no_validate, echo=typer.echo)
  except RemoteError as exc:
    typer.echo(f"remote_error: {exc}")
    raise typer.Exit(code=2)
  raise typer.Exit(code=code)


@app.command("logs")
def logs_cmd(
  host: str = typer.Argument(..., help="The robot: user@host or an ssh config name."),
  project: Path = typer.Option(Path("."), "--project", help="Project folder (its name picks the service)."),
  lines: int = typer.Option(100, "--lines", "-n", help="How many earlier lines to show."),
  no_follow: bool = typer.Option(False, "--no-follow", help="Print and exit instead of following."),
) -> None:
  """Show the robot's service logs (from `nervlynx deploy --service`)."""
  from robot_core.remote import RemoteError, logs

  try:
    raise typer.Exit(code=logs(_target(host, project, None, "nervlynx"), follow=not no_follow, lines=lines))
  except RemoteError as exc:
    typer.echo(f"remote_error: {exc}")
    raise typer.Exit(code=2)


@app.command("pull")
def pull_cmd(
  host: str = typer.Argument(..., help="The robot: user@host or an ssh config name."),
  project: Path = typer.Option(Path("."), "--project", help="Project folder (its name picks the folder on the robot)."),
  path: Optional[str] = typer.Option(None, "--path", help="Project folder on the robot (default: ~/nervlynx-projects/<name>)."),
  destination: Optional[Path] = typer.Option(None, "--to", help="Where to put the runs (default: logs/robot/<host>/)."),
) -> None:
  """Copy the runs recorded on the robot (reports and traces) back to this computer."""
  from robot_core.remote import RemoteError, pull

  target = _target(host, project, path, "nervlynx")
  dest = destination or project / "logs" / "robot" / host.split("@")[-1]
  try:
    code = pull(target, dest)
  except RemoteError as exc:
    typer.echo(f"remote_error: {exc}")
    raise typer.Exit(code=2)
  if code == 0:
    typer.echo(f"runs from {host} are in {dest}")
  raise typer.Exit(code=code)


fleet_app = typer.Typer(help="Deploy one project to many robots (fleet.yaml), check them, and roll back.")
app.add_typer(fleet_app, name="fleet")
_ROBOTS_OPTION = typer.Option(None, "--robots", help="Only these robots, comma-separated (default: all in fleet.yaml).")


def _fleet(project: Path, robots: Optional[str], **kwargs):
  from robot_core.fleet import Fleet, load_fleet, select
  from robot_core.remote import RemoteError

  try:
    chosen = select(load_fleet(project), robots)
  except RemoteError as exc:
    typer.echo(f"fleet_error: {exc}")
    raise typer.Exit(code=2)
  return Fleet(project, chosen, echo=typer.echo, **kwargs)


def _fleet_done(results: list) -> None:
  from robot_core.fleet import render

  typer.echo("\n" + render(results))
  raise typer.Exit(code=0 if all(r.get("ok") for r in results) else 1)


@fleet_app.command("list")
def fleet_list(project: Path = typer.Option(Path("."), "--project", help="Project folder with robot.yaml and fleet.yaml.")) -> None:
  """List the robots in fleet.yaml (no network)."""
  fleet = _fleet(project, None)
  for robot in fleet.robots:
    overlay = f"  overlay {robot.overlay.relative_to(project)}" if robot.overlay else ""
    typer.echo(f"  {robot.name:<16} {robot.host}{overlay}")


@fleet_app.command("deploy")
def fleet_deploy(
  project: Path = typer.Option(Path("."), "--project", help="Project folder with robot.yaml and fleet.yaml."),
  robots: Optional[str] = _ROBOTS_OPTION,
  no_rollback: bool = typer.Option(False, "--no-rollback", help="Leave a robot on the new release even if it comes up unhealthy."),
  parallel: int = typer.Option(4, "--parallel", help="How many robots to work on at once."),
  health_timeout_s: float = typer.Option(20.0, "--health-timeout-s", help="How long a restarted robot has to report healthy."),
) -> None:
  """Copy the project to every robot, restart it, and roll back any robot that comes up unhealthy."""
  if not (project / "robot.yaml").exists():
    typer.echo(f"fleet_error: {project} has no robot.yaml")
    raise typer.Exit(code=2)
  _fleet_done(_fleet(project, robots, parallel=parallel, health_timeout_s=health_timeout_s).deploy(rollback=not no_rollback))


@fleet_app.command("status")
def fleet_status(
  project: Path = typer.Option(Path("."), "--project", help="Project folder with fleet.yaml."),
  robots: Optional[str] = _ROBOTS_OPTION,
) -> None:
  """Show each robot's service, health, release, and NervLynx version."""
  _fleet_done(_fleet(project, robots).status())


@fleet_app.command("rollback")
def fleet_rollback(
  project: Path = typer.Option(Path("."), "--project", help="Project folder with fleet.yaml."),
  robots: Optional[str] = _ROBOTS_OPTION,
  to: Optional[str] = typer.Option(None, "--to", help="Release id to go back to (default: the one before the current)."),
) -> None:
  """Put robots back on an earlier release (kept on each robot by fleet deploy)."""
  _fleet_done(_fleet(project, robots).rollback(to))


@fleet_app.command("upgrade")
def fleet_upgrade(
  project: Path = typer.Option(Path("."), "--project", help="Project folder with fleet.yaml."),
  robots: Optional[str] = _ROBOTS_OPTION,
  ref: str = typer.Option("main", "--ref", help="Branch, tag, or commit of NervLynx to install."),
  extras: str = typer.Option("", "--extras", help="Extras to include, e.g. ai,mesh."),
  spec: Optional[str] = typer.Option(None, "--spec", help="Exact pip requirement to install instead."),
) -> None:
  """Update NervLynx itself on every robot over the air, then restart and check each one."""
  suffix = f"[{extras}]" if extras else ""
  requirement = spec or f"nervlynx{suffix} @ git+https://github.com/vedantparnaik/nervlynx@{ref}"
  _fleet_done(_fleet(project, robots).upgrade(requirement))


@app.command("scan")
def scan_cmd(
  bus: int = typer.Option(1, "--bus", help="I2C bus number (1 on a Raspberry Pi)."),
  output: Optional[Path] = typer.Option(None, "--output", "-o", help="Also write the suggested nodes to this file."),
  as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
  """Find attached sensors (I2C, USB serial, cameras) and suggest nodes for robot.yaml."""
  from robot_core.scan import draft_nodes_yaml, scan

  found, notes = scan(bus_number=bus)
  if as_json:
    typer.echo(json.dumps({"found": [item.to_dict() for item in found], "notes": notes}, indent=2))
    return
  if not found:
    typer.echo("nothing found on I2C, USB serial, or cameras")
  for item in found:
    driver = f"  -> plugin: {item.node}" if item.node else ""
    typer.echo(f"  {item.kind:<6} {item.where:<28} {item.name}{driver}")
  for note in notes:
    typer.echo(f"  note: {note}")
  draft = draft_nodes_yaml(found)
  if any(item.node for item in found):
    typer.echo("\n" + draft)
  if output is not None:
    output.write_text(draft, encoding="utf-8")
    typer.echo(f"wrote {output}")


@app.command("models")
def models_cmd(
  action: str = typer.Argument("list", help="list, or get."),
  name: Optional[str] = typer.Argument(None, help="The model to download with get, e.g. yolox-nano."),
) -> None:
  """List the named detection models, or download one ahead of time (nervlynx models get yolox-nano)."""
  from robot_core.detect import MODELS, fetch_model, model_dir
  from robot_core.hardware import HardwareUnavailable

  if action == "list":
    for info in MODELS.values():
      state = "downloaded" if (model_dir() / f"{info.name}.onnx").is_file() else "not downloaded"
      typer.echo(f"  {info.name:<12} {info.size_mb:5.1f} MB  {info.license:<11} {state}")
    typer.echo(f"cache: {model_dir()} (set NERVLYNX_MODEL_DIR to move it)")
    return
  if action != "get" or not name:
    typer.echo("usage: nervlynx models [list] | nervlynx models get <name>")
    raise typer.Exit(code=2)
  try:
    path = fetch_model(name, echo=typer.echo)
  except (ValueError, HardwareUnavailable) as exc:
    typer.echo(f"models_error: {exc}")
    raise typer.Exit(code=1)
  typer.echo(f"{name}: {path}")


@app.command("version")
def version() -> None:
  """Print the installed nervlynx package version."""
  from importlib.metadata import PackageNotFoundError, version as pkg_version

  try:
    typer.echo(pkg_version("nervlynx"))
  except PackageNotFoundError:
    typer.echo("nervlynx-unknown")


if __name__ == "__main__":
  app()
