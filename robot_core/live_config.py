"""Load, validate, and build live graph configs. Schema reference: docs/LIVE_RUNTIME.md."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable

import yaml

from robot_core.hardware import BACKENDS
from robot_core.live import ESTOP_TOPIC, LiveNode, LiveRuntime, PluginNodeAdapter, SensorSourceNode
from robot_core.metrics import MetricsRegistry
from robot_core.plugins import LazyFactory, PluginRegistry
from robot_core.runtime import Clock, SimulatedClock, SystemClock

# Imported only when a config uses them.
LIVE_BUILTINS: dict[str, str] = {
  "skid_steer_drive": "robot_core.drive:SkidSteerDrive",
  "scripted_drive": "robot_core.sim:ScriptedDriveSource",
  "skid_steer_sim": "robot_core.sim:SkidSteerSim",
  "hcsr04_range": "robot_core.sensors:HCSR04Range",
  "mpu6050_imu": "robot_core.sensors:MPU6050Imu",
  "camera": "robot_core.camera:CameraNode",
  "esp32_link": "robot_core.link:Esp32Link",
}

_TOP_LEVEL_KEYS = {"name", "description", "runtime", "safety", "hardware", "nodes"}
_RUNTIME_KEYS = {
  "clock",
  "max_queue_size",
  "max_hops_per_step",
  "stall_timeout_s",
  "max_idle_sleep_s",
  "breaker",
  "topic_priority",
  "seed",
  "latest_topics",
  "max_inbox_size",
}
_SAFETY_KEYS = {"stale_after_s", "estop_on_stale", "start_in_estop"}
_HARDWARE_KEYS = {"backend"}
_NODE_KEYS = {"name", "plugin", "input_topics", "rate_hz", "critical", "stale_after_s", "topic", "schema", "params", "only"}
MODES = ("robot", "sim")


def select_mode(cfg: dict[str, Any], mode: str) -> dict[str, Any]:
  """Copy of `cfg` with only the nodes that run in `mode`; nodes without `only` run in both."""
  nodes = cfg.get("nodes")
  if not isinstance(nodes, list):
    return dict(cfg)
  kept = [n for n in nodes if not isinstance(n, dict) or n.get("only") in (None, mode)]
  return {**cfg, "nodes": kept}


def uses_modes(cfg: dict[str, Any]) -> bool:
  nodes = cfg.get("nodes")
  return isinstance(nodes, list) and any(isinstance(n, dict) and "only" in n for n in nodes)


def register_live_builtins(registry: PluginRegistry) -> None:
  for name, path in LIVE_BUILTINS.items():
    registry.register_live_node(name, LazyFactory(path))


def load_live_config(path: str | Path) -> dict[str, Any]:
  with Path(path).open("r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
  if not isinstance(raw, dict):
    raise ValueError(f"{path}: live config must be a YAML mapping")
  return raw


def _is_name(value: object) -> bool:
  return isinstance(value, str) and bool(value.strip())


def _is_number(value: object) -> bool:
  return isinstance(value, (int, float)) and not isinstance(value, bool)


def _section(cfg: dict[str, Any], key: str) -> dict[str, Any]:
  value = cfg.get(key) or {}
  return value if isinstance(value, dict) else {}


def _accepts(factory: Callable[..., Any], param: str) -> bool:
  try:
    params = inspect.signature(factory).parameters
  except (TypeError, ValueError):
    return False
  return param in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _node_params(
  factory: Callable[..., Any],
  params: dict[str, Any],
  cfg: dict[str, Any],
  backend_override: str | None,
) -> dict[str, Any]:
  out = dict(params)
  if _accepts(factory, "backend"):
    if backend_override:
      out["backend"] = backend_override
    elif "backend" not in out and _section(cfg, "hardware").get("backend"):
      out["backend"] = _section(cfg, "hardware")["backend"]
  return out


def validate_live_config(
  cfg: dict[str, Any],
  registry: PluginRegistry,
  *,
  backend_override: str | None = None,
  mode: str | None = None,
) -> list[str]:
  """Return human-readable problems; an empty list means the config can be built.

  With `mode`, only the nodes that run in that mode are checked (see `select_mode`).
  """
  if not isinstance(cfg, dict):
    return ["config must be a mapping"]
  issues: list[str] = []
  if mode is not None and mode not in MODES:
    return [f"mode must be one of {', '.join(MODES)}"]
  for idx, node_cfg in enumerate(cfg.get("nodes") if isinstance(cfg.get("nodes"), list) else []):
    if isinstance(node_cfg, dict) and "only" in node_cfg and node_cfg["only"] not in MODES:
      issues.append(f"nodes[{idx}].only must be 'sim' or 'robot'")
  for key in sorted(set(cfg) - _TOP_LEVEL_KEYS):
    issues.append(f"unknown top-level key: {key}")
  if "name" in cfg and not _is_name(cfg["name"]):
    issues.append("name must be a non-empty string")

  for key, allowed in (("runtime", _RUNTIME_KEYS), ("safety", _SAFETY_KEYS), ("hardware", _HARDWARE_KEYS)):
    if key in cfg and not isinstance(cfg[key], dict):
      issues.append(f"{key} must be a mapping")
      continue
    for extra in sorted(set(_section(cfg, key)) - allowed):
      issues.append(f"unknown {key} key: {extra}")

  runtime = _section(cfg, "runtime")
  if runtime.get("clock", "system") not in ("system", "simulated"):
    issues.append("runtime.clock must be 'system' or 'simulated'")
  for key in ("max_queue_size", "max_hops_per_step", "max_inbox_size"):
    if key in runtime and (not isinstance(runtime[key], int) or isinstance(runtime[key], bool) or runtime[key] < 1):
      issues.append(f"runtime.{key} must be a positive integer")
  for key in ("stall_timeout_s", "max_idle_sleep_s"):
    if key in runtime and runtime[key] is not None and (not _is_number(runtime[key]) or runtime[key] <= 0):
      issues.append(f"runtime.{key} must be a positive number")
  if "seed" in runtime and (not isinstance(runtime["seed"], int) or isinstance(runtime["seed"], bool)):
    issues.append("runtime.seed must be an integer")
  breaker = runtime.get("breaker", {})
  if not isinstance(breaker, dict):
    issues.append("runtime.breaker must be a mapping")
  else:
    for extra in sorted(set(breaker) - {"threshold", "cooldown_s"}):
      issues.append(f"unknown runtime.breaker key: {extra}")
    if "threshold" in breaker and (not isinstance(breaker["threshold"], int) or isinstance(breaker["threshold"], bool) or breaker["threshold"] < 0):
      issues.append("runtime.breaker.threshold must be an integer >= 0 (0 disables the breaker)")
    if "cooldown_s" in breaker and (not _is_number(breaker["cooldown_s"]) or breaker["cooldown_s"] <= 0):
      issues.append("runtime.breaker.cooldown_s must be a positive number")
  latest = runtime.get("latest_topics", [])
  if not isinstance(latest, list) or not all(_is_name(t) for t in latest):
    issues.append("runtime.latest_topics must be a list of topic names")
  priority = runtime.get("topic_priority", {})
  if not isinstance(priority, dict) or not all(_is_name(k) and isinstance(v, int) and not isinstance(v, bool) for k, v in priority.items()):
    issues.append("runtime.topic_priority must map topic names to integers")

  safety = _section(cfg, "safety")
  if "stale_after_s" in safety and (not _is_number(safety["stale_after_s"]) or safety["stale_after_s"] <= 0):
    issues.append("safety.stale_after_s must be a positive number")
  for key in ("estop_on_stale", "start_in_estop"):
    if key in safety and not isinstance(safety[key], bool):
      issues.append(f"safety.{key} must be true or false")

  backend = _section(cfg, "hardware").get("backend")
  if backend is not None and backend not in BACKENDS:
    issues.append(f"hardware.backend must be one of {', '.join(BACKENDS)}")
  if backend_override is not None and backend_override not in BACKENDS:
    issues.append(f"backend override must be one of {', '.join(BACKENDS)}")

  nodes = cfg.get("nodes")
  if not isinstance(nodes, list) or not nodes:
    issues.append("nodes must be a non-empty list")
    return issues
  if mode is not None and not select_mode(cfg, mode)["nodes"]:
    issues.append(f"no nodes run in {mode} mode")
    return issues

  seen: set[str] = set()
  for idx, node_cfg in enumerate(nodes):
    prefix = f"nodes[{idx}]"
    if not isinstance(node_cfg, dict):
      issues.append(f"{prefix} must be a mapping")
      continue
    if mode is not None and node_cfg.get("only") not in (None, mode):
      continue
    plugin = node_cfg.get("plugin")
    if not _is_name(plugin):
      issues.append(f"{prefix}.plugin must be a non-empty string")
      continue
    name = node_cfg.get("name", plugin)
    if not _is_name(name):
      issues.append(f"{prefix}.name must be a non-empty string")
      continue
    prefix = f"{prefix} ({name})"
    if name in seen:
      issues.append(f"{prefix}: duplicate node name")
    seen.add(name)
    for extra in sorted(set(node_cfg) - _NODE_KEYS):
      issues.append(f"{prefix}: unknown key {extra}")
    topics = node_cfg.get("input_topics")
    if topics is not None and (not isinstance(topics, list) or not all(_is_name(t) for t in topics)):
      issues.append(f"{prefix}.input_topics must be a list of non-empty strings")
    rate = node_cfg.get("rate_hz")
    if rate is not None and (not _is_number(rate) or rate <= 0):
      issues.append(f"{prefix}.rate_hz must be a positive number")
    if "critical" in node_cfg and not isinstance(node_cfg["critical"], bool):
      issues.append(f"{prefix}.critical must be true or false")
    stale = node_cfg.get("stale_after_s")
    if stale is not None and (not _is_number(stale) or stale <= 0):
      issues.append(f"{prefix}.stale_after_s must be a positive number")
    params = node_cfg.get("params", {})
    if not isinstance(params, dict):
      issues.append(f"{prefix}.params must be a mapping")
      params = {}

    if registry.has_live_node(plugin):
      factory = registry.get_live_node_factory(plugin)
      try:
        instance = factory(**_node_params(factory, params, cfg, backend_override))
      except TypeError as exc:
        issues.append(f"{prefix}: invalid params for {plugin}: {exc}")
      except ValueError as exc:
        issues.append(f"{prefix}: {exc}")
      else:
        effective_topics = topics if topics is not None else list(getattr(instance, "input_topics", ()) or ())
        if rate is None and getattr(instance, "rate_hz", None) is None and not effective_topics:
          issues.append(f"{prefix}: has no input_topics and no rate_hz, so it would never run")
    elif registry.has_node(plugin):
      if params:
        issues.append(f"{prefix}: params are only supported for live node plugins")
      plugin_topics = topics if topics is not None else list(getattr(registry.get_node(plugin), "input_topics", None) or [])
      if not plugin_topics:
        issues.append(f"{prefix}: node plugin needs input_topics")
    elif registry.has_sensor(plugin):
      if rate is None:
        issues.append(f"{prefix}: sensor plugins need rate_hz")
      if not _is_name(node_cfg.get("topic")):
        issues.append(f"{prefix}: sensor plugins need an output topic")
      if "schema" in node_cfg and not _is_name(node_cfg["schema"]):
        issues.append(f"{prefix}.schema must be a non-empty string")
    else:
      issues.append(f"{prefix}.plugin not found in registry: {plugin}")
  return issues


def clock_for_config(cfg: dict[str, Any], *, simulated: bool = False) -> Clock:
  if simulated or _section(cfg, "runtime").get("clock") == "simulated":
    return SimulatedClock()
  return SystemClock()


def build_live_runtime(
  cfg: dict[str, Any],
  registry: PluginRegistry,
  *,
  clock: Clock | None = None,
  metrics: MetricsRegistry | None = None,
  backend_override: str | None = None,
  mode: str | None = None,
) -> LiveRuntime:
  issues = validate_live_config(cfg, registry, backend_override=backend_override, mode=mode)
  if issues:
    raise ValueError("invalid live config: " + "; ".join(issues))
  if mode is not None:
    cfg = select_mode(cfg, mode)
  runtime_cfg = _section(cfg, "runtime")
  safety = _section(cfg, "safety")
  breaker = runtime_cfg.get("breaker") or {}
  clock = clock or clock_for_config(cfg)
  seed = runtime_cfg.get("seed")
  if seed is None and clock.simulated:
    seed = 0
  priority = {ESTOP_TOPIC: 0, **(runtime_cfg.get("topic_priority") or {})}
  runtime = LiveRuntime(
    name=str(cfg.get("name", "live")),
    clock=clock,
    metrics=metrics,
    max_queue_size=int(runtime_cfg.get("max_queue_size", 4096)),
    topic_priority=priority,
    max_hops_per_step=int(runtime_cfg.get("max_hops_per_step", 4096)),
    breaker_threshold=int(breaker.get("threshold", 5)),
    breaker_cooldown_s=float(breaker.get("cooldown_s", 2.0)),
    stale_after_s=safety.get("stale_after_s", 0.5),
    estop_on_stale=bool(safety.get("estop_on_stale", True)),
    stall_timeout_s=runtime_cfg.get("stall_timeout_s", 0.5),
    max_idle_sleep_s=float(runtime_cfg.get("max_idle_sleep_s", 0.05)),
    seed=seed,
    latest_topics=runtime_cfg.get("latest_topics") or (),
    max_inbox_size=int(runtime_cfg.get("max_inbox_size", 4096)),
  )
  for node_cfg in cfg["nodes"]:
    plugin = node_cfg["plugin"]
    name = node_cfg.get("name", plugin)
    topics = node_cfg.get("input_topics")
    if registry.has_live_node(plugin):
      factory = registry.get_live_node_factory(plugin)
      node: LiveNode = factory(**_node_params(factory, node_cfg.get("params") or {}, cfg, backend_override))
    elif registry.has_node(plugin):
      node = PluginNodeAdapter(registry.get_node(plugin), topics)
    else:
      node = SensorSourceNode(registry.get_sensor(plugin), str(node_cfg["topic"]), str(node_cfg.get("schema", "SensorReading")))
    runtime.add_node(
      name,
      node,
      input_topics=topics,
      rate_hz=node_cfg.get("rate_hz", getattr(node, "rate_hz", None)),
      critical=bool(node_cfg.get("critical", getattr(node, "critical", False))),
      stale_after_s=node_cfg.get("stale_after_s"),
    )
  if safety.get("start_in_estop"):
    runtime.request_estop("start_in_estop is set in the config", source="config")
  return runtime
