"""Reusable robotics runtime skeleton."""

from robot_core.builtin_plugins import register_builtin_plugins
from robot_core.checkpoint import CheckpointStore
from robot_core.chaos import ChaosConfig, run_chaos_pass
from robot_core.codegen import load_contract_idl, run_codegen
from robot_core.contracts import TopicContract, check_contract_migration, default_contracts, validate_payload
from robot_core.dashboard import serve_dashboard, snapshot_runtime
from robot_core.distributed import DistributedNodeConfig, DistributedNodeRunner
from robot_core.graph import load_graph_config, wire_graph_from_config
from robot_core.metrics import MetricsRegistry, serve_metrics
from robot_core.observability import flow_stats, structured_event, timeline_by_trace, topic_latency_stats
from robot_core.plugins import PluginRegistry
from robot_core.runtime import AsyncPipelineRuntime, Envelope, PipelineRuntime, RuntimeMessage, SimulatedClock, SystemClock
from robot_core.security import TopicAccessPolicy, sign_payload, verify_payload_signature
from robot_core.smoke_matrix import run_smoke_matrix
from robot_core.smoke_surveillance import run_surveillance_smoke
from robot_core.supervisor import ManagedNode, RuntimeSupervisor
from robot_core.transport import InMemoryTransport, ZmqJsonTransport
from robot_core.watchdog import HealthWatchdog

__all__ = [
  "Envelope",
  "RuntimeMessage",
  "PipelineRuntime",
  "AsyncPipelineRuntime",
  "SystemClock",
  "SimulatedClock",
  "HealthWatchdog",
  "PluginRegistry",
  "register_builtin_plugins",
  "CheckpointStore",
  "ChaosConfig",
  "run_chaos_pass",
  "load_contract_idl",
  "run_codegen",
  "RuntimeSupervisor",
  "ManagedNode",
  "DistributedNodeConfig",
  "DistributedNodeRunner",
  "InMemoryTransport",
  "ZmqJsonTransport",
  "TopicAccessPolicy",
  "sign_payload",
  "verify_payload_signature",
  "MetricsRegistry",
  "serve_metrics",
  "serve_dashboard",
  "snapshot_runtime",
  "TopicContract",
  "default_contracts",
  "validate_payload",
  "check_contract_migration",
  "load_graph_config",
  "wire_graph_from_config",
  "timeline_by_trace",
  "topic_latency_stats",
  "flow_stats",
  "structured_event",
  "run_smoke_matrix",
  "run_surveillance_smoke",
]
