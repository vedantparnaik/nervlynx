from pathlib import Path
from urllib.request import urlopen

from robot_core.builtin_plugins import register_builtin_plugins
from robot_core.checkpoint import CheckpointStore
from robot_core.chaos import ChaosConfig, inject_faults
from robot_core.dashboard import serve_dashboard
from robot_core.distributed import DistributedNodeConfig, DistributedNodeRunner
from robot_core.metrics import MetricsRegistry
from robot_core.plugins import PluginRegistry
from robot_core.runtime import PipelineRuntime
from robot_core.security import TopicAccessPolicy, sign_payload, verify_payload_signature
from robot_core.transport import InMemoryTransport


def test_sign_and_verify_payload() -> None:
  payload = {"a": 1, "b": True}
  sig = sign_payload(payload, "secret")
  assert verify_payload_signature(payload, sig, "secret")
  assert not verify_payload_signature(payload, sig, "other")


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
  store = CheckpointStore(tmp_path / "ckpt")
  store.save("planner", {"mode": "patrol", "n": 1})
  loaded = store.load("planner")
  assert loaded and loaded["mode"] == "patrol"


def test_chaos_inject_drop_or_mutate() -> None:
  payload = {"camera_count": 4, "gps_fix": True}
  out = inject_faults(payload, ChaosConfig(drop_probability=0.0, mutate_probability=1.0, seed=1))
  assert out is not None
  assert "camera_count" in out


def test_dashboard_endpoints() -> None:
  rt = PipelineRuntime()
  metrics = MetricsRegistry()
  server = serve_dashboard(rt, metrics, host="127.0.0.1", port=9121)
  try:
    health = urlopen("http://127.0.0.1:9121/health", timeout=2).read().decode("utf-8")
    graph = urlopen("http://127.0.0.1:9121/graph", timeout=2).read().decode("utf-8")
  finally:
    server.shutdown()
  assert "ok" in health
  assert "subscriptions" in graph


def test_distributed_node_runner_inmemory() -> None:
  transport = InMemoryTransport()
  reg = PluginRegistry()
  register_builtin_plugins(reg)
  cfg = DistributedNodeConfig(
    node_name="worker",
    plugin_name="perception_node",
    subscribe_topics=("sensors.bundle",),
    policy=TopicAccessPolicy(
      allowed_publish_topics=("perception.scene",),
      allowed_subscribe_topics=("sensors.bundle",),
    ),
    secret="local-dev-secret",
  )
  runner = DistributedNodeRunner(transport, reg, cfg)
  runner.start()
  outputs: list[str] = []
  transport.subscribe("perception.scene", lambda m: outputs.append(m.envelope.topic))
  payload = {"camera_count": 4, "gps_fix": True, "imu_ok": True, "ai_ok": True}
  signed = dict(payload)
  signed["_signature"] = sign_payload(payload, "local-dev-secret")
  seed = PipelineRuntime().publish(topic="sensors.bundle", source="s", schema="SensorBundle", payload=signed)
  transport.publish(seed)
  assert outputs == ["perception.scene"]
