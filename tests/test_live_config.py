from pathlib import Path

import pytest

from robot_core.builtin_plugins import register_builtin_plugins
from robot_core.live_config import build_live_runtime, load_live_config, register_live_builtins, validate_live_config
from robot_core.plugins import PluginRegistry
from robot_core.reference_plugins import register_reference_plugins
from robot_core.runtime import SimulatedClock

LIVE_EXAMPLES = sorted(Path("examples/live").glob("*.yaml"))


@pytest.fixture()
def reg() -> PluginRegistry:
  registry = PluginRegistry()
  register_builtin_plugins(registry)
  register_reference_plugins(registry)
  register_live_builtins(registry)
  return registry


def test_live_examples_exist() -> None:
  names = {p.name for p in LIVE_EXAMPLES}
  assert {"rover_sim.yaml", "rover_bts7960.yaml", "rover_tb6612.yaml", "surveillance_live.yaml"} <= names


@pytest.mark.parametrize("path", LIVE_EXAMPLES, ids=lambda p: p.name)
def test_every_live_example_validates(path: Path, reg: PluginRegistry) -> None:
  assert validate_live_config(load_live_config(path), reg) == []


def test_hardware_examples_build_with_mock_backend_override(reg: PluginRegistry) -> None:
  for name in ("rover_bts7960.yaml", "rover_tb6612.yaml"):
    cfg = load_live_config(Path("examples/live") / name)
    rt = build_live_runtime(cfg, reg, clock=SimulatedClock(), backend_override="mock")
    assert rt.nodes["drive"].backend_name == "mock"
    rt.run(duration_s=0.5)
    assert rt.snapshot()["health"]["status"] == "ok"


def test_hardware_backend_comes_from_the_hardware_section(reg: PluginRegistry) -> None:
  cfg = load_live_config("examples/live/rover_bts7960.yaml")
  rt = build_live_runtime(cfg, reg, clock=SimulatedClock(), backend_override=None)
  assert rt.nodes["drive"].backend_name == "rpi_gpio"


def test_rover_sim_closes_the_loop(reg: PluginRegistry) -> None:
  rt = build_live_runtime(load_live_config("examples/live/rover_sim.yaml"), reg, clock=SimulatedClock())
  rt.run(duration_s=12.0)
  snap = rt.snapshot()
  assert snap["health"]["status"] == "ok"
  assert snap["nodes"]["plant"]["status"]["distance_m"] > 2.0
  assert snap["nodes"]["drive"]["status"]["kicks"]["left"] >= 2
  assert snap["messages"]["dropped"] == 0
  assert snap["topics"]["drive.state"]["latency_ms"]["max"] <= 20.0


def test_surveillance_live_runs_legacy_plugins_continuously(reg: PluginRegistry) -> None:
  rt = build_live_runtime(load_live_config("examples/live/surveillance_live.yaml"), reg, clock=SimulatedClock())
  rt.run(duration_s=2.0)
  topics = rt.snapshot()["topics"]
  assert topics["sensors.bundle"]["count"] == 21
  assert topics["mission.command"]["count"] == 21
  assert topics["actuator.feedback"]["count"] == 21


def _base(**node) -> dict:
  return {"nodes": [{"name": "drive", "plugin": "skid_steer_drive", "rate_hz": 50, **node}]}


def test_validation_reports_useful_errors(reg: PluginRegistry) -> None:
  bad = {
    "name": "x",
    "surprise": 1,
    "runtime": {"clock": "wall", "breaker": {"threshold": -1}, "max_queue_size": 0},
    "safety": {"estop_on_stale": "yes"},
    "hardware": {"backend": "arduino"},
    "nodes": [
      {"name": "a", "plugin": "scripted_drive", "rate_hz": 0, "params": {"steps": []}},
      {"name": "a", "plugin": "perception_node"},
      {"name": "cam", "plugin": "camera_ingest_sensor"},
      {"name": "ghost", "plugin": "does_not_exist"},
      {"name": "legacy", "plugin": "perception_node", "params": {"x": 1}},
    ],
  }
  issues = validate_live_config(bad, reg)
  joined = "\n".join(issues)
  for expected in (
    "unknown top-level key: surprise",
    "runtime.clock must be 'system' or 'simulated'",
    "runtime.breaker.threshold must be an integer >= 0",
    "runtime.max_queue_size must be a positive integer",
    "safety.estop_on_stale must be true or false",
    "hardware.backend must be one of mock, rpi_gpio, gpiozero",
    "nodes[0] (a).rate_hz must be a positive number",
    "nodes[0] (a): steps must be a non-empty list",
    "nodes[1] (a): duplicate node name",
    "nodes[2] (cam): sensor plugins need rate_hz",
    "nodes[2] (cam): sensor plugins need an output topic",
    "nodes[3] (ghost).plugin not found in registry: does_not_exist",
    "nodes[4] (legacy): params are only supported for live node plugins",
  ):
    assert expected in joined


def test_validation_catches_bad_node_params(reg: PluginRegistry) -> None:
  cfg = _base(params={"left": [{"name": "A", "rpwm": 1, "lpwm": 2}], "right": [{"name": "B", "rpwm": 2, "lpwm": 3}]})
  assert any("pin 2 is used by both" in issue for issue in validate_live_config(cfg, reg))
  cfg = _base(params={"left": [], "right": [], "colour": "red"})
  assert any("invalid params for skid_steer_drive" in issue for issue in validate_live_config(cfg, reg))
  assert validate_live_config({"nodes": []}, reg) == ["nodes must be a non-empty list"]


def test_start_in_estop(reg: PluginRegistry) -> None:
  cfg = load_live_config("examples/live/rover_sim.yaml")
  cfg["safety"] = {"start_in_estop": True}
  rt = build_live_runtime(cfg, reg, clock=SimulatedClock())
  rt.run(duration_s=1.0)
  snap = rt.snapshot()
  assert snap["estop"]["engaged"] is True
  assert snap["nodes"]["plant"]["status"]["distance_m"] == 0.0
