from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TopicContract:
  topic: str
  schema: str
  version: int
  required_fields: tuple[str, ...]


@dataclass(frozen=True)
class ContractIssue:
  topic: str
  message: str


def validate_payload(contract: TopicContract, payload: dict[str, Any]) -> list[ContractIssue]:
  issues: list[ContractIssue] = []
  for field in contract.required_fields:
    if field not in payload:
      issues.append(ContractIssue(topic=contract.topic, message=f"missing required field: {field}"))
  return issues


def check_contract_migration(old: TopicContract, new: TopicContract) -> list[ContractIssue]:
  issues: list[ContractIssue] = []
  if old.topic != new.topic:
    issues.append(ContractIssue(topic=new.topic, message="topic rename is not backward compatible"))
  if new.version < old.version:
    issues.append(ContractIssue(topic=new.topic, message="schema version cannot decrease"))
  removed = set(old.required_fields) - set(new.required_fields)
  if removed:
    issues.append(
      ContractIssue(
        topic=new.topic,
        message="required fields removed without compatibility strategy: " + ",".join(sorted(removed)),
      )
    )
  return issues


def default_contracts() -> dict[str, TopicContract]:
  return {
    "sensors.bundle": TopicContract(
      topic="sensors.bundle",
      schema="SensorBundle",
      version=1,
      required_fields=("camera_count", "gps_fix", "imu_ok", "ai_ok"),
    ),
    "perception.scene": TopicContract(
      topic="perception.scene",
      schema="SceneState",
      version=1,
      required_fields=("confidence", "motion_detected", "audio_event", "gps_fix"),
    ),
    "mission.command": TopicContract(
      topic="mission.command",
      schema="MissionCommand",
      version=1,
      required_fields=("mode", "speed_limit_mps", "waypoint_id"),
    ),
  }
