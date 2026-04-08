from __future__ import annotations

from robot_core.runtime import PipelineRuntime, RuntimeMessage


def build_reference_runtime() -> PipelineRuntime:
  """Small, generic Sensor Ingest -> Fusion -> Planning flow."""
  rt = PipelineRuntime()

  def fusion_handler(msg: RuntimeMessage):
    if msg.envelope.topic != "sensors.raw":
      return []
    payload = msg.payload
    camera_count = int(payload.get("camera_count", 0))
    gps_fix = bool(payload.get("gps_fix", False))
    quality = min(1.0, 0.2 + 0.1 * camera_count + (0.3 if gps_fix else 0.0))
    return [("perception.state", "PerceptionState", {"quality": quality, "gps_fix": gps_fix})]

  def planner_handler(msg: RuntimeMessage):
    if msg.envelope.topic != "perception.state":
      return []
    quality = float(msg.payload["quality"])
    mission_state = "go" if quality >= 0.6 else "slow"
    target_speed_mps = 3.0 if mission_state == "go" else 1.0
    return [("control.intent", "ControlIntent", {"state": mission_state, "target_speed_mps": target_speed_mps})]

  rt.subscribe(node_name="fusion", topic="sensors.raw", handler=fusion_handler)
  rt.subscribe(node_name="planner", topic="perception.state", handler=planner_handler)
  return rt
