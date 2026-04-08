from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json


@dataclass(frozen=True)
class SensorFrame:
  speed_mps: float
  yaw_rate_rps: float
  wheel_odom_m: float
  lat_deg: float
  lon_deg: float
  heading_deg: float


class SensorAdapter:
  def read(self) -> SensorFrame:
    raise NotImplementedError


class SyntheticSensorAdapter(SensorAdapter):
  def __init__(self, state_hz: float = 100.0) -> None:
    self._wheel_odom = 0.0
    self._speed_mps = 3.6
    self._state_hz = max(1.0, state_hz)

  def read(self) -> SensorFrame:
    self._wheel_odom += self._speed_mps / self._state_hz
    return SensorFrame(
      speed_mps=self._speed_mps,
      yaw_rate_rps=0.01,
      wheel_odom_m=self._wheel_odom,
      lat_deg=37.335,
      lon_deg=-121.89,
      heading_deg=88.0,
    )


class ReplaySensorAdapter(SensorAdapter):
  def __init__(self, path: str | Path) -> None:
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
      self._rows = [json.loads(line) for line in f if line.strip()]
    if not self._rows:
      raise ValueError("replay sensor input is empty")
    self._idx = 0

  def read(self) -> SensorFrame:
    row = self._rows[self._idx]
    self._idx = (self._idx + 1) % len(self._rows)
    return SensorFrame(
      speed_mps=float(row["speed_mps"]),
      yaw_rate_rps=float(row.get("yaw_rate_rps", 0.0)),
      wheel_odom_m=float(row["wheel_odom_m"]),
      lat_deg=float(row["lat_deg"]),
      lon_deg=float(row["lon_deg"]),
      heading_deg=float(row["heading_deg"]),
    )
