from pathlib import Path

from robot_core.smoke_surveillance import run_surveillance_smoke


def test_surveillance_smoke_passes(tmp_path: Path) -> None:
  result = run_surveillance_smoke(output_path=tmp_path / "surveillance_trace.jsonl")
  assert result.ok is True
  assert result.message_count >= 6
  assert "hq.telemetry" in result.topics
  assert "hq.alert" in result.topics
  assert result.output_path.exists()
