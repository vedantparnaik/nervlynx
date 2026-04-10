from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class GeneratedContract:
  topic: str
  schema: str
  version: int
  required_fields: tuple[str, ...]

CONTRACTS = [
  GeneratedContract(topic='sensors.bundle', schema='SensorBundle', version=1, required_fields=('camera_count', 'gps_fix', 'imu_ok', 'ai_ok')),
  GeneratedContract(topic='perception.scene', schema='SceneState', version=1, required_fields=('confidence', 'motion_detected', 'audio_event', 'gps_fix')),
  GeneratedContract(topic='mission.command', schema='MissionCommand', version=1, required_fields=('mode', 'speed_limit_mps', 'waypoint_id')),
]
