from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shuttle.schema import load_schema


@dataclass(frozen=True)
class MessageDef:
  topic: str
  schema: str
  cls: Any


def message_defs() -> dict[str, MessageDef]:
  s = load_schema()
  defs = {
    "vehicleState": MessageDef("vehicleState", "VehicleState", s.VehicleState),
    "routeProgress": MessageDef("routeProgress", "RouteProgress", s.RouteProgress),
    "plannerTrajectory": MessageDef("plannerTrajectory", "PlannerTrajectory", s.PlannerTrajectory),
    "controlCommand": MessageDef("controlCommand", "ControlCommand", s.ControlCommand),
    "heartbeat": MessageDef("heartbeat", "Heartbeat", s.Heartbeat),
    "faultEvent": MessageDef("faultEvent", "FaultEvent", s.FaultEvent),
    "safetyState": MessageDef("safetyState", "SafetyState", s.SafetyState),
  }
  return defs
