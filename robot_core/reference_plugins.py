from __future__ import annotations

from dataclasses import dataclass

from robot_core.plugins import NodePlugin, PluginRegistry, SensorPlugin
from robot_core.runtime import RuntimeMessage


@dataclass(frozen=True)
class CameraIngestSensorPlugin(SensorPlugin):
  name: str = "camera_ingest_sensor"

  def read(self) -> dict[str, object]:
    return {
      "camera_count": 6,
      "gps_fix": True,
      "imu_ok": True,
      "ai_ok": True,
      "frame_rate_hz": 20,
    }


@dataclass(frozen=True)
class PlannerStubNodePlugin(NodePlugin):
  name: str = "planner_stub_node"
  input_topics: list[str] = None  # type: ignore[assignment]

  def __post_init__(self) -> None:
    object.__setattr__(self, "input_topics", ["perception.scene"])

  def handle(self, msg: RuntimeMessage) -> list[tuple[str, str, dict[str, object]]]:
    confidence = float(msg.payload.get("confidence", 0.0))
    mode = "go" if confidence >= 0.75 else "safe_slow"
    return [
      (
        "mission.command",
        "MissionCommand",
        {"mode": mode, "speed_limit_mps": 1.2 if mode == "go" else 0.6, "reason": "planner_stub"},
      )
    ]


@dataclass(frozen=True)
class ActuatorMockNodePlugin(NodePlugin):
  name: str = "actuator_mock_node"
  input_topics: list[str] = None  # type: ignore[assignment]

  def __post_init__(self) -> None:
    object.__setattr__(self, "input_topics", ["mission.command"])

  def handle(self, msg: RuntimeMessage) -> list[tuple[str, str, dict[str, object]]]:
    return [
      (
        "actuator.feedback",
        "ActuatorFeedback",
        {
          "accepted": True,
          "mode": str(msg.payload.get("mode", "unknown")),
          "origin": "actuator_mock",
        },
      )
    ]


def register_reference_plugins(reg: PluginRegistry) -> None:
  reg.register_sensor(CameraIngestSensorPlugin())
  reg.register_node(PlannerStubNodePlugin())
  reg.register_node(ActuatorMockNodePlugin())
