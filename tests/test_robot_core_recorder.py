from pathlib import Path

from robot_core.examples import build_reference_runtime
from robot_core.recorder import read_jsonl, write_jsonl


def test_record_and_replay_roundtrip(tmp_path: Path) -> None:
  rt = build_reference_runtime()
  seed = rt.publish(
    topic="sensors.raw",
    source="sensor_adapter",
    schema="RawSensors",
    payload={"camera_count": 6, "gps_fix": True},
  )
  trace = rt.run_once(seed)
  out = tmp_path / "trace.jsonl"
  write_jsonl(out, trace)
  loaded = read_jsonl(out)
  assert len(loaded) == len(trace)
  assert [m.envelope.topic for m in loaded] == [m.envelope.topic for m in trace]
  assert loaded[-1].payload["target_speed_mps"] == trace[-1].payload["target_speed_mps"]
