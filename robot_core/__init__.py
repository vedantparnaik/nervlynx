"""Reusable robotics runtime skeleton.

Exports load on first use, so `import robot_core.live` (or `from nervlynx import node`)
does not import the whole package; that start-up time matters on a Pi Zero 2 W.
"""

from importlib import import_module
from typing import Any

_EXPORTS = {
  "Envelope": "robot_core.runtime",
  "RuntimeMessage": "robot_core.runtime",
  "PipelineRuntime": "robot_core.runtime",
  "AsyncPipelineRuntime": "robot_core.runtime",
  "SystemClock": "robot_core.runtime",
  "SimulatedClock": "robot_core.runtime",
  "HealthWatchdog": "robot_core.watchdog",
  "PluginRegistry": "robot_core.plugins",
  "register_builtin_plugins": "robot_core.builtin_plugins",
  "register_reference_plugins": "robot_core.reference_plugins",
  "CheckpointStore": "robot_core.checkpoint",
  "ChaosConfig": "robot_core.chaos",
  "run_chaos_pass": "robot_core.chaos",
  "load_contract_idl": "robot_core.codegen",
  "run_codegen": "robot_core.codegen",
  "RuntimeSupervisor": "robot_core.supervisor",
  "ManagedNode": "robot_core.supervisor",
  "DistributedNodeConfig": "robot_core.distributed",
  "DistributedNodeRunner": "robot_core.distributed",
  "InMemoryTransport": "robot_core.transport",
  "ZmqJsonTransport": "robot_core.transport",
  "TopicAccessPolicy": "robot_core.security",
  "sign_payload": "robot_core.security",
  "verify_payload_signature": "robot_core.security",
  "MetricsRegistry": "robot_core.metrics",
  "serve_metrics": "robot_core.metrics",
  "serve_dashboard": "robot_core.dashboard",
  "snapshot_runtime": "robot_core.dashboard",
  "TopicContract": "robot_core.contracts",
  "default_contracts": "robot_core.contracts",
  "validate_payload": "robot_core.contracts",
  "check_contract_migration": "robot_core.contracts",
  "load_graph_config": "robot_core.graph",
  "wire_graph_from_config": "robot_core.graph",
  "timeline_by_trace": "robot_core.observability",
  "topic_latency_stats": "robot_core.observability",
  "flow_stats": "robot_core.observability",
  "structured_event": "robot_core.observability",
  "run_smoke_matrix": "robot_core.smoke_matrix",
  "run_surveillance_smoke": "robot_core.smoke_surveillance",
  "LiveRuntime": "robot_core.live",
  "LiveNode": "robot_core.live",
  "NodeContext": "robot_core.live",
  "LiveRuntimeError": "robot_core.live",
  "FaultEvent": "robot_core.live",
  "build_live_runtime": "robot_core.live_config",
  "load_live_config": "robot_core.live_config",
  "validate_live_config": "robot_core.live_config",
  "register_live_builtins": "robot_core.live_config",
  "serve_live": "robot_core.server",
  "SkidSteerDrive": "robot_core.drive",
  "DriveTuning": "robot_core.drive",
  "ScriptedDriveSource": "robot_core.sim",
  "SkidSteerSim": "robot_core.sim",
  "BTS7960Motor": "robot_core.hardware",
  "TB6612Motor": "robot_core.hardware",
  "MockBackend": "robot_core.hardware",
  "create_backend": "robot_core.hardware",
  "Histogram": "robot_core.metrics",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
  module = _EXPORTS.get(name)
  if module is None:
    raise AttributeError(f"module 'robot_core' has no attribute {name!r}")
  value = getattr(import_module(module), name)
  globals()[name] = value
  return value


def __dir__() -> list[str]:
  return sorted(set(globals()) | set(_EXPORTS))
