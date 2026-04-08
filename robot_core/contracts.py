from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TopicContract:
  topic: str
  schema: str
  version: int
  required_fields: tuple[str, ...]
  field_types: dict[str, tuple[type, ...]] | None = None
  allow_additional_fields: bool = True


@dataclass(frozen=True)
class ContractIssue:
  topic: str
  message: str


def validate_payload(contract: TopicContract, payload: dict[str, Any]) -> list[ContractIssue]:
  issues: list[ContractIssue] = []
  for field in contract.required_fields:
    if field not in payload:
      issues.append(ContractIssue(topic=contract.topic, message=f"missing required field: {field}"))
  if contract.field_types:
    for field, allowed in contract.field_types.items():
      if field not in payload:
        continue
      if not isinstance(payload[field], allowed):
        names = ",".join(t.__name__ for t in allowed)
        issues.append(
          ContractIssue(
            topic=contract.topic,
            message=f"invalid type for {field}: expected {names}, got {type(payload[field]).__name__}",
          )
        )
  if not contract.allow_additional_fields:
    allowed_fields = set(contract.required_fields)
    extras = sorted(set(payload.keys()) - allowed_fields)
    for extra in extras:
      issues.append(ContractIssue(topic=contract.topic, message=f"unknown field: {extra}"))
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
  if old.field_types and new.field_types:
    for k, old_types in old.field_types.items():
      if k not in new.field_types:
        continue
      if not set(new.field_types[k]).issuperset(set(old_types)):
        issues.append(
          ContractIssue(
            topic=new.topic,
            message=f"type narrowing for field {k} is not backward compatible",
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
      field_types={
        "camera_count": (int, float),
        "gps_fix": (bool,),
        "imu_ok": (bool,),
        "ai_ok": (bool,),
      },
    ),
    "perception.scene": TopicContract(
      topic="perception.scene",
      schema="SceneState",
      version=1,
      required_fields=("confidence", "motion_detected", "audio_event", "gps_fix"),
      field_types={
        "confidence": (int, float),
        "motion_detected": (bool,),
        "audio_event": (bool,),
        "gps_fix": (bool,),
      },
    ),
    "mission.command": TopicContract(
      topic="mission.command",
      schema="MissionCommand",
      version=1,
      required_fields=("mode", "speed_limit_mps", "waypoint_id"),
      field_types={
        "mode": (str,),
        "speed_limit_mps": (int, float),
        "waypoint_id": (str,),
      },
    ),
  }
