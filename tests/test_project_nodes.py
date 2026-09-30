from pathlib import Path

from typer.testing import CliRunner

from robot_core.cli import app
from robot_core.live_config import build_live_runtime, validate_live_config
from robot_core.project import load_project

ROBOT_YAML = """\
name: {name}
nodes:
  - name: pattern
    plugin: scripted_drive
    rate_hz: 20
    params:
      steps: [{{linear: 0.5, angular: 0.0, duration_s: 1.0}}]
  - plugin: {plugin}
"""


def make_project(root: Path, name: str, nodes: dict[str, str], plugin: str) -> Path:
  (root / "nodes").mkdir(parents=True)
  for filename, source in nodes.items():
    (root / "nodes" / filename).write_text(source, encoding="utf-8")
  config = root / "robot.yaml"
  config.write_text(ROBOT_YAML.format(name=name, plugin=plugin), encoding="utf-8")
  return config


def test_nodes_folder_functions_become_plugins(tmp_path: Path) -> None:
  config = make_project(
    tmp_path,
    "halver",
    {
      "halve_speed.py": (
        "from nervlynx import node\n\n\n"
        "@node(inputs='cmd.drive', outputs='cmd.half')\n"
        "def halve_speed(cmd, *, factor=0.5):\n"
        "  return {'linear': cmd['linear'] * factor}\n"
      )
    },
    "halve_speed",
  )
  cfg, reg, problems = load_project(config)
  assert problems == []
  assert validate_live_config(cfg, reg) == []
  from robot_core.runtime import SimulatedClock

  rt = build_live_runtime(cfg, reg, clock=SimulatedClock())
  seen: list = []
  rt.subscribe("probe", "cmd.half", seen.append)
  rt.run(duration_s=0.5)
  assert seen and seen[-1].payload == {"linear": 0.25}


def test_load_problems_are_reported_not_raised(tmp_path: Path) -> None:
  config = make_project(
    tmp_path,
    "broken",
    {
      "broken_syntax.py": "def oops(:\n",
      "clash_builtin.py": (
        "from nervlynx import node\n\n\n"
        "@node(name='skid_steer_drive', outputs='x', rate_hz=1)\n"
        "def mine():\n"
        "  return 1\n"
      ),
      "dup_one.py": "from nervlynx import node\n\n\n@node(name='twice', outputs='x', rate_hz=1)\ndef first():\n  return 1\n",
      "dup_two.py": "from nervlynx import node\n\n\n@node(name='twice', outputs='x', rate_hz=1)\ndef second():\n  return 2\n",
    },
    "twice",
  )
  _, reg, problems = load_project(config)
  joined = "\n".join(problems)
  assert "broken_syntax.py: failed to load: SyntaxError" in joined
  assert "'skid_steer_drive' clashes with a built-in plugin" in joined
  assert "'twice' is also defined in dup_one.py" in joined
  assert reg.has_live_node("twice")


def test_validation_flags_nodes_that_would_never_run(tmp_path: Path) -> None:
  config = make_project(
    tmp_path,
    "idle",
    {"idle_node.py": "from nervlynx import node\n\n\n@node(outputs='x')\ndef idle_node():\n  return 1\n"},
    "idle_node",
  )
  cfg, reg, _ = load_project(config)
  assert "nodes[1] (idle_node): has no input_topics and no rate_hz, so it would never run" in validate_live_config(cfg, reg)


def test_live_validate_cli_sees_project_nodes(tmp_path: Path) -> None:
  config = make_project(
    tmp_path,
    "cli_project",
    {"echo_cmd.py": "from nervlynx import node\n\n\n@node(inputs='cmd.drive', outputs='cmd.echo')\ndef echo_cmd(cmd):\n  return cmd\n"},
    "echo_cmd",
  )
  result = CliRunner().invoke(app, ["live-validate", str(config)])
  assert result.exit_code == 0, result.stdout
  assert "live_config_valid=true nodes=2" in result.stdout
