from __future__ import annotations

from dataclasses import dataclass

from robot_core.plugins import NodePlugin, PluginRegistry, SensorPlugin
from robot_core.runtime import RuntimeMessage


@dataclass(frozen=True)
class SyntheticSensorPlugin(SensorPlugin):
  name: str = "synthetic_surveillance_sensor"

  def read(self) -> dict[str, object]:
    return {
      "camera_count": 4,
      "gps_fix": True,
      "imu_ok": True,
      "ai_ok": True,
      "motion_detected": False,
      "mic_anomaly": False,
    }


@dataclass(frozen=True)
class PerceptionNodePlugin(NodePlugin):
  name: str = "perception_node"
  input_topics: list[str] = None  # type: ignore[assignment]

  def __post_init__(self) -> None:
    object.__setattr__(self, "input_topics", ["sensors.bundle"])

  def handle(self, msg: RuntimeMessage) -> list[tuple[str, str, dict[str, object]]]:
    conf = 0.6 + (0.1 if bool(msg.payload.get("gps_fix", False)) else 0.0)
    return [("perception.scene", "SceneState", {"confidence": conf, "gps_fix": bool(msg.payload.get("gps_fix", False))})]


@dataclass(frozen=True)
class PlannerNodePlugin(NodePlugin):
  name: str = "planner_node"
  input_topics: list[str] = None  # type: ignore[assignment]

  def __post_init__(self) -> None:
    object.__setattr__(self, "input_topics", ["perception.scene"])

  def handle(self, msg: RuntimeMessage) -> list[tuple[str, str, dict[str, object]]]:
    mode = "patrol" if float(msg.payload.get("confidence", 0.0)) >= 0.7 else "safe_slow"
    return [("mission.command", "MissionCommand", {"mode": mode, "speed_limit_mps": 1.0, "waypoint_id": "wp-a"})]


def register_builtin_plugins(reg: PluginRegistry) -> None:
  reg.register_sensor(SyntheticSensorPlugin())
  reg.register_node(PerceptionNodePlugin())
  reg.register_node(PlannerNodePlugin())
