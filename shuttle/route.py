from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RoutePoint:
  distance_m: float
  speed_mps: float


@dataclass(frozen=True)
class RouteMap:
  name: str
  loop: bool
  points: list[RoutePoint]

  @property
  def length_m(self) -> float:
    return self.points[-1].distance_m if self.points else 0.0


@dataclass(frozen=True)
class RouteSnapshot:
  route_distance_m: float
  route_length_m: float
  route_percent: float
  target_speed_mps: float


def load_route(path: str | Path) -> RouteMap:
  p = Path(path)
  with p.open("r", encoding="utf-8") as f:
    raw = yaml.safe_load(f)
  points = [
    RoutePoint(distance_m=float(item["distance_m"]), speed_mps=float(item["speed_mps"]))
    for item in raw["points"]
  ]
  points.sort(key=lambda x: x.distance_m)
  if len(points) < 2:
    raise ValueError("route must contain at least 2 points")
  return RouteMap(name=str(raw.get("name", "route")), loop=bool(raw.get("loop", True)), points=points)


def _normalize_distance(route: RouteMap, odom_m: float) -> float:
  if route.length_m <= 0.0:
    return 0.0
  if route.loop:
    return odom_m % route.length_m
  return max(0.0, min(route.length_m, odom_m))


def speed_at_distance(route: RouteMap, route_distance_m: float) -> float:
  d = _normalize_distance(route, route_distance_m)
  points = route.points
  for i in range(len(points) - 1):
    a = points[i]
    b = points[i + 1]
    if a.distance_m <= d <= b.distance_m:
      span = max(1e-6, b.distance_m - a.distance_m)
      t = (d - a.distance_m) / span
      return a.speed_mps + t * (b.speed_mps - a.speed_mps)
  return points[-1].speed_mps


def snapshot_from_odom(route: RouteMap, wheel_odom_m: float) -> RouteSnapshot:
  d = _normalize_distance(route, wheel_odom_m)
  length = max(route.length_m, 1e-6)
  pct = float(d / length)
  return RouteSnapshot(
    route_distance_m=d,
    route_length_m=route.length_m,
    route_percent=pct,
    target_speed_mps=speed_at_distance(route, d),
  )
