from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from robot_core.generated_contracts import CONTRACTS

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
  out: dict[str, TopicContract] = {}
  for c in CONTRACTS:
    out[c.topic] = TopicContract(
      topic=c.topic,
      schema=c.schema,
      version=c.version,
      required_fields=c.required_fields,
    )
  # Add runtime type hints for known built-ins.
  if "sensors.bundle" in out:
    out["sensors.bundle"] = TopicContract(
      **{**out["sensors.bundle"].__dict__, "field_types": {"camera_count": (int, float), "gps_fix": (bool,), "imu_ok": (bool,), "ai_ok": (bool,)}}
    )
  if "perception.scene" in out:
    out["perception.scene"] = TopicContract(
      **{
        **out["perception.scene"].__dict__,
        "field_types": {"confidence": (int, float), "motion_detected": (bool,), "audio_event": (bool,), "gps_fix": (bool,)},
      }
    )
  if "mission.command" in out:
    out["mission.command"] = TopicContract(
      **{
        **out["mission.command"].__dict__,
        "field_types": {"mode": (str,), "speed_limit_mps": (int, float), "waypoint_id": (str,)},
      }
    )
  return out
