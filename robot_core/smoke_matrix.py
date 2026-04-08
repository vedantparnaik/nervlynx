from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from robot_core.smoke_surveillance import run_surveillance_smoke


@dataclass(frozen=True)
class SmokeCase:
  name: str
  payload_overrides: dict[str, object]
  require_alert: bool


@dataclass(frozen=True)
class SmokeCaseResult:
  name: str
  ok: bool
  topics: list[str]
  message_count: int
  output_path: Path


def default_smoke_cases() -> list[SmokeCase]:
  return [
    SmokeCase(name="nominal", payload_overrides={}, require_alert=True),
    SmokeCase(name="gps_loss", payload_overrides={"gps_fix": False}, require_alert=True),
    SmokeCase(name="camera_drop", payload_overrides={"camera_count": 1}, require_alert=True),
    SmokeCase(name="imu_fault", payload_overrides={"imu_ok": False}, require_alert=True),
    SmokeCase(name="wifi_low", payload_overrides={"wifi_rssi_dbm": -90}, require_alert=True),
  ]


def run_smoke_matrix(output_dir: str | Path = "logs/smoke_matrix") -> list[SmokeCaseResult]:
  out_dir = Path(output_dir)
  out_dir.mkdir(parents=True, exist_ok=True)
  results: list[SmokeCaseResult] = []
  for case in default_smoke_cases():
    out = out_dir / f"{case.name}.jsonl"
    result = run_surveillance_smoke(
      output_path=out,
      sensor_payload=case.payload_overrides,
      require_alert=case.require_alert,
    )
    results.append(
      SmokeCaseResult(
        name=case.name,
        ok=result.ok,
        topics=result.topics,
        message_count=result.message_count,
        output_path=result.output_path,
      )
    )
  return results
