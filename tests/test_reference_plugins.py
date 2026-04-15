from robot_core.reference_plugins import ActuatorMockNodePlugin, CameraIngestSensorPlugin, PlannerStubNodePlugin
from robot_core.runtime import Envelope, RuntimeMessage


def test_camera_ingest_sensor_plugin_shape() -> None:
  payload = CameraIngestSensorPlugin().read()
  assert payload["camera_count"] == 6
  assert payload["gps_fix"] is True
  assert payload["frame_rate_hz"] == 20


def test_planner_stub_and_actuator_mock_flow() -> None:
  planner = PlannerStubNodePlugin()
  actuator = ActuatorMockNodePlugin()
  scene = RuntimeMessage(
    envelope=Envelope(
      topic="perception.scene",
      source="perception",
      sequence=1,
      monotonic_time_ns=1,
      trace_id="rpack",
      schema="SceneState",
    ),
    payload={"confidence": 0.91},
  )
  mission = planner.handle(scene)[0]
  assert mission[0] == "mission.command"
  assert mission[2]["mode"] == "go"

  command = RuntimeMessage(
    envelope=Envelope(
      topic="mission.command",
      source="planner_stub",
      sequence=1,
      monotonic_time_ns=2,
      trace_id="rpack",
      schema="MissionCommand",
    ),
    payload=mission[2],
  )
  feedback = actuator.handle(command)[0]
  assert feedback[0] == "actuator.feedback"
  assert feedback[2]["accepted"] is True
