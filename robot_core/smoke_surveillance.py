from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from robot_core.recorder import write_jsonl
from robot_core.runtime import PipelineRuntime, RuntimeMessage
from robot_core.watchdog import HealthWatchdog


@dataclass(frozen=True)
class SmokeResult:
  ok: bool
  message_count: int
  topics: list[str]
  watchdog_faults: list[str]
  output_path: Path


def build_surveillance_runtime() -> PipelineRuntime:
  rt = PipelineRuntime()

  def perception_handler(msg: RuntimeMessage):
    if msg.envelope.topic != "sensors.bundle":
      return []
    p = msg.payload
    cameras_ok = int(p.get("camera_count", 0)) >= 3
    imu_ok = bool(p.get("imu_ok", False))
    gps_fix = bool(p.get("gps_fix", False))
    ai_ok = bool(p.get("ai_ok", False))
    confidence = 0.3
    if cameras_ok:
      confidence += 0.25
    if imu_ok:
      confidence += 0.2
    if gps_fix:
      confidence += 0.15
    if ai_ok:
      confidence += 0.1
    motion_detected = bool(p.get("motion_detected", False))
    audio_event = bool(p.get("mic_anomaly", False))
    return [
      (
        "perception.scene",
        "SceneState",
        {
          "confidence": min(1.0, confidence),
          "motion_detected": motion_detected,
          "audio_event": audio_event,
          "gps_fix": gps_fix,
        },
      )
    ]

  def mission_handler(msg: RuntimeMessage):
    if msg.envelope.topic != "perception.scene":
      return []
    confidence = float(msg.payload["confidence"])
    gps_fix = bool(msg.payload["gps_fix"])
    motion_detected = bool(msg.payload["motion_detected"])
    patrol_mode = "autonomous_patrol" if confidence >= 0.7 and gps_fix else "safe_slow_patrol"
    speed_limit_mps = 1.4 if patrol_mode == "autonomous_patrol" else 0.6
    events: list[tuple[str, str, dict[str, object]]] = [
      (
        "mission.command",
        "MissionCommand",
        {
          "mode": patrol_mode,
          "speed_limit_mps": speed_limit_mps,
          "waypoint_id": "colony-sector-c",
        },
      )
    ]
    if motion_detected or bool(msg.payload["audio_event"]):
      events.append(
        (
          "hq.alert",
          "SecurityAlert",
          {
            "severity": "medium",
            "event": "intrusion_candidate",
            "needs_operator_ack": True,
          },
        )
      )
    return events

  def control_handler(msg: RuntimeMessage):
    if msg.envelope.topic != "mission.command":
      return []
    speed_limit_mps = float(msg.payload["speed_limit_mps"])
    return [
      (
        "actuation.command",
        "WheelControl",
        {
          "left_motor_pct": min(0.5, speed_limit_mps / 3.0),
          "right_motor_pct": min(0.5, speed_limit_mps / 3.0),
          "brake_pct": 0.0,
        },
      )
    ]

  def uplink_handler(msg: RuntimeMessage):
    if msg.envelope.topic != "actuation.command":
      return []
    return [
      (
        "hq.telemetry",
        "TelemetryUplink",
        {
          "link": "wifi",
          "robot_id": "sr-3w-demo",
          "status": "online",
          "battery_pct": 74.0,
        },
      )
    ]

  rt.subscribe("perception_ai", "sensors.bundle", perception_handler)
  rt.subscribe("mission_manager", "perception.scene", mission_handler)
  rt.subscribe("drive_controller", "mission.command", control_handler)
  rt.subscribe("hq_uplink", "actuation.command", uplink_handler)
  return rt


DEFAULT_SURVEILLANCE_PAYLOAD: dict[str, Any] = {
  "camera_count": 4,
  "gps_fix": True,
  "imu_ok": True,
  "ai_ok": True,
  "wifi_rssi_dbm": -61,
  "motion_detected": True,
  "mic_anomaly": False,
  "speaker_ok": True,
}


def run_surveillance_smoke(
  output_path: str | Path = "logs/smoke_surveillance_trace.jsonl",
  sensor_payload: dict[str, Any] | None = None,
  require_alert: bool = True,
) -> SmokeResult:
  rt = build_surveillance_runtime()
  payload = dict(DEFAULT_SURVEILLANCE_PAYLOAD)
  if sensor_payload:
    payload.update(sensor_payload)
  seed = rt.publish(
    topic="sensors.bundle",
    source="sensor_hub",
    schema="SensorBundle",
    payload=payload,
  )
  trace = rt.run_once(seed)
  out = Path(output_path)
  write_jsonl(out, trace)
  hb = rt.node_heartbeats_ns
  faults = HealthWatchdog(stale_after_s=0.75).check(hb, now_ns=max(hb.values()))
  topics = [m.envelope.topic for m in trace]
  required_topics = {
    "sensors.bundle",
    "perception.scene",
    "mission.command",
    "actuation.command",
    "hq.telemetry",
  }
  if require_alert:
    required_topics.add("hq.alert")
  ok = required_topics.issubset(set(topics)) and len(faults) == 0 and len(trace) >= 6
  return SmokeResult(
    ok=ok,
    message_count=len(trace),
    topics=topics,
    watchdog_faults=[f.message for f in faults],
    output_path=out,
  )
