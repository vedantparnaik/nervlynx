from robot_core.live_config import build_live_runtime, select_mode, uses_modes, validate_live_config
from robot_core.project import build_registry
from robot_core.runtime import SimulatedClock

STEPS = {"steps": [{"linear": 0.3, "angular": 0.0, "duration_s": 1.0}]}
CFG = {
  "name": "modes",
  "nodes": [
    {"name": "pattern", "plugin": "scripted_drive", "rate_hz": 20, "params": STEPS},
    {"name": "driver", "plugin": "scripted_drive", "rate_hz": 20, "params": STEPS, "only": "robot"},
    {"name": "plant", "plugin": "skid_steer_sim", "rate_hz": 50, "only": "sim"},
  ],
}


def test_select_mode_keeps_shared_nodes_and_that_modes_nodes() -> None:
  assert [n["name"] for n in select_mode(CFG, "sim")["nodes"]] == ["pattern", "plant"]
  assert [n["name"] for n in select_mode(CFG, "robot")["nodes"]] == ["pattern", "driver"]
  assert uses_modes(CFG) and not uses_modes({"nodes": [{"plugin": "x"}]})


def test_validation_per_mode_keeps_file_indices() -> None:
  reg = build_registry()
  cfg = {
    **CFG,
    "nodes": CFG["nodes"]
    + [
      {"name": "broken_driver", "plugin": "scripted_drive", "rate_hz": 20, "params": {"steps": []}, "only": "robot"},
      {"name": "ghost", "plugin": "nope", "only": "sim"},
    ],
  }
  assert validate_live_config(cfg, reg, mode="robot") == ["nodes[3] (broken_driver): steps must be a non-empty list"]
  assert validate_live_config(cfg, reg, mode="sim") == ["nodes[4] (ghost).plugin not found in registry: nope"]
  assert validate_live_config(CFG, reg, mode="robot") == [] and validate_live_config(CFG, reg, mode="sim") == []


def test_bad_only_values_and_empty_modes_are_reported() -> None:
  reg = build_registry()
  typo = {"nodes": [{"plugin": "skid_steer_sim", "rate_hz": 50, "only": "simulation"}]}
  assert "nodes[0].only must be 'sim' or 'robot'" in validate_live_config(typo, reg, mode="sim")
  sim_only = {"nodes": [{"plugin": "skid_steer_sim", "rate_hz": 50, "only": "sim"}]}
  assert validate_live_config(sim_only, reg, mode="robot") == ["no nodes run in robot mode"]


def test_build_in_a_mode_only_instantiates_that_modes_nodes() -> None:
  reg = build_registry()
  rt = build_live_runtime(CFG, reg, clock=SimulatedClock(), mode="sim")
  assert sorted(rt.nodes) == ["pattern", "plant"]
  rt = build_live_runtime(CFG, reg, clock=SimulatedClock(), mode="robot")
  assert sorted(rt.nodes) == ["driver", "pattern"]
