"""Simulation nodes: scripted command sources, a skid-steer plant model, and a 2D world.

They let a full sense -> command -> actuate -> motion loop run on a laptop or in CI with
the mock hardware backend, and give soak tests a repeatable workload. With a `world`, the
plant collides with walls and obstacles and simulated range sensors see them, so
obstacle-avoidance code can be developed before the robot exists.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Iterable

from robot_core.lidar import ray_scan, scan_payload
from robot_core.live import LiveNode, NodeContext, Output
from robot_core.runtime import RuntimeMessage

_EPS = 1e-12
_LIDAR_KEYS = {"topic", "bins", "range_min_m", "range_max_m", "noise_m", "every_n_ticks"}
_CONTACT_RELEASE_M = 0.01
_BEAM_RAYS = 9


@dataclass(frozen=True)
class _Circle:
  x: float
  y: float
  r: float


@dataclass(frozen=True)
class _Box:
  x0: float
  y0: float
  x1: float
  y1: float


def _numbers(raw: Any, count: int, label: str) -> list[float]:
  if not isinstance(raw, (list, tuple)) or len(raw) != count or not all(
    isinstance(v, (int, float)) and not isinstance(v, bool) for v in raw
  ):
    raise ValueError(f"{label} must be a list of {count} numbers")
  return [float(v) for v in raw]


def _ray_walls(ox: float, oy: float, dx: float, dy: float, width: float, height: float) -> float:
  hits = []
  if dx > _EPS:
    hits.append((width - ox) / dx)
  elif dx < -_EPS:
    hits.append(-ox / dx)
  if dy > _EPS:
    hits.append((height - oy) / dy)
  elif dy < -_EPS:
    hits.append(-oy / dy)
  return max(0.0, min(hits)) if hits else math.inf


def _ray_circle(ox: float, oy: float, dx: float, dy: float, c: _Circle) -> float | None:
  fx, fy = ox - c.x, oy - c.y
  inside = fx * fx + fy * fy - c.r * c.r
  if inside <= 0.0:
    return 0.0
  b = fx * dx + fy * dy
  disc = b * b - inside
  if disc < 0.0:
    return None
  t = -b - math.sqrt(disc)
  return t if t >= 0.0 else None


def _ray_box(ox: float, oy: float, dx: float, dy: float, box: _Box) -> float | None:
  t_near, t_far = -math.inf, math.inf
  for origin, direction, lo, hi in ((ox, dx, box.x0, box.x1), (oy, dy, box.y0, box.y1)):
    if abs(direction) < _EPS:
      if origin < lo or origin > hi:
        return None
      continue
    t1, t2 = (lo - origin) / direction, (hi - origin) / direction
    if t1 > t2:
      t1, t2 = t2, t1
    t_near, t_far = max(t_near, t1), min(t_far, t2)
    if t_near > t_far:
      return None
  if t_far < 0.0:
    return None
  return max(t_near, 0.0)


class SimWorld:
  """Rectangular arena with walls at x in [0, width] and y in [0, height], plus obstacles.

  Obstacles are `{circle: [x, y, r]}` or `{box: [x_min, y_min, x_max, y_max]}`, in metres.
  """

  def __init__(self, width_m: float, height_m: float, obstacles: Iterable[_Circle | _Box] = ()) -> None:
    if width_m <= 0 or height_m <= 0:
      raise ValueError("world width_m and height_m must be > 0")
    self.width_m = float(width_m)
    self.height_m = float(height_m)
    self.obstacles = tuple(obstacles)

  @classmethod
  def from_dict(cls, raw: Any) -> SimWorld:
    if not isinstance(raw, dict):
      raise ValueError("world must be a mapping")
    unknown = sorted(set(raw) - {"width_m", "height_m", "obstacles"})
    if unknown:
      raise ValueError(f"unknown world fields: {', '.join(unknown)}")
    obstacles: list[_Circle | _Box] = []
    for idx, item in enumerate(raw.get("obstacles") or []):
      if not isinstance(item, dict) or len(item) != 1 or next(iter(item)) not in ("circle", "box"):
        raise ValueError(f"world.obstacles[{idx}] must be {{circle: [x, y, r]}} or {{box: [x_min, y_min, x_max, y_max]}}")
      kind, values = next(iter(item.items()))
      if kind == "circle":
        x, y, r = _numbers(values, 3, f"world.obstacles[{idx}].circle")
        if r <= 0:
          raise ValueError(f"world.obstacles[{idx}].circle radius must be > 0")
        obstacles.append(_Circle(x, y, r))
      else:
        x0, y0, x1, y1 = _numbers(values, 4, f"world.obstacles[{idx}].box")
        if x1 <= x0 or y1 <= y0:
          raise ValueError(f"world.obstacles[{idx}].box needs x_min < x_max and y_min < y_max")
        obstacles.append(_Box(x0, y0, x1, y1))
    width = raw.get("width_m", 4.0)
    height = raw.get("height_m", 3.0)
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (width, height)):
      raise ValueError("world width_m and height_m must be numbers")
    return cls(width, height, obstacles)

  def ray(self, x: float, y: float, heading_rad: float, max_range_m: float) -> float:
    """Distance from (x, y) along heading to the first wall or obstacle, capped at max_range_m."""
    dx, dy = math.cos(heading_rad), math.sin(heading_rad)
    best = _ray_walls(x, y, dx, dy, self.width_m, self.height_m)
    for obstacle in self.obstacles:
      hit = _ray_circle(x, y, dx, dy, obstacle) if isinstance(obstacle, _Circle) else _ray_box(x, y, dx, dy, obstacle)
      if hit is not None and hit < best:
        best = hit
    return min(best, max_range_m)

  def collision(self, x: float, y: float, radius: float) -> str | None:
    """What a circular robot at (x, y) overlaps, or None."""
    if x - radius < 0.0 or y - radius < 0.0 or x + radius > self.width_m or y + radius > self.height_m:
      return "a wall"
    for idx, obstacle in enumerate(self.obstacles):
      if isinstance(obstacle, _Circle):
        if math.hypot(x - obstacle.x, y - obstacle.y) < obstacle.r + radius:
          return f"obstacle {idx}"
      else:
        cx = min(max(x, obstacle.x0), obstacle.x1)
        cy = min(max(y, obstacle.y0), obstacle.y1)
        if math.hypot(x - cx, y - cy) < radius:
          return f"obstacle {idx}"
    return None

  def to_dict(self) -> dict[str, Any]:
    return {
      "width_m": self.width_m,
      "height_m": self.height_m,
      "obstacles": [
        {"circle": [o.x, o.y, o.r]} if isinstance(o, _Circle) else {"box": [o.x0, o.y0, o.x1, o.y1]} for o in self.obstacles
      ],
    }


@dataclass(frozen=True)
class RangeSensorSpec:
  """A simulated ranger. Ultrasonic sensors report the nearest echo inside a cone
  (`beam_deg`, about 30 for an HC-SR04); 0 models a single ray like a narrow ToF sensor."""

  name: str
  angle_rad: float
  max_range_m: float
  noise_m: float
  topic: str
  beam_rad: float = 0.0

  @classmethod
  def parse(cls, raw: Any, idx: int) -> RangeSensorSpec:
    if not isinstance(raw, dict):
      raise ValueError(f"range_sensors[{idx}] must be a mapping")
    unknown = sorted(set(raw) - {"name", "angle_deg", "max_range_m", "noise_m", "topic", "beam_deg"})
    if unknown:
      raise ValueError(f"range_sensors[{idx}]: unknown fields {', '.join(unknown)}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
      raise ValueError(f"range_sensors[{idx}].name must be a non-empty string")
    max_range = float(raw.get("max_range_m", 2.0))
    noise = float(raw.get("noise_m", 0.0))
    beam = float(raw.get("beam_deg", 0.0))
    if max_range <= 0 or noise < 0:
      raise ValueError(f"range_sensors[{idx}] ({name}): max_range_m must be > 0 and noise_m >= 0")
    if not 0.0 <= beam <= 120.0:
      raise ValueError(f"range_sensors[{idx}] ({name}): beam_deg must be between 0 and 120")
    return cls(
      name,
      math.radians(float(raw.get("angle_deg", 0.0))),
      max_range,
      noise,
      str(raw.get("topic", f"range.{name}")),
      math.radians(beam),
    )

  def ray_angles(self) -> list[float]:
    if self.beam_rad == 0.0:
      return [self.angle_rad]
    half = self.beam_rad / 2
    return [self.angle_rad - half + self.beam_rad * i / (_BEAM_RAYS - 1) for i in range(_BEAM_RAYS)]


class ScriptedDriveSource(LiveNode):
  """Publishes a timed sequence of drive commands, optionally looping.

  Each step is {"left": l, "right": r, "duration_s": d} or {"linear": v, "angular": w, "duration_s": d}.
  After a non-looping script ends the source goes quiet, so the drive's deadman stops the
  robot, which is itself a useful thing to exercise.
  """

  def __init__(
    self,
    *,
    steps: list[dict[str, Any]],
    topic: str = "cmd.drive",
    loop: bool = True,
    start_delay_s: float = 0.0,
  ) -> None:
    if not isinstance(steps, list) or not steps:
      raise ValueError("steps must be a non-empty list")
    parsed: list[tuple[dict[str, float], float]] = []
    for idx, step in enumerate(steps):
      if not isinstance(step, dict):
        raise ValueError(f"steps[{idx}] must be a mapping")
      duration = float(step.get("duration_s", 0.0))
      if duration <= 0:
        raise ValueError(f"steps[{idx}].duration_s must be > 0")
      if "left" in step and "right" in step:
        command = {"left": float(step["left"]), "right": float(step["right"])}
      elif "linear" in step:
        command = {"linear": float(step["linear"]), "angular": float(step.get("angular", 0.0))}
      else:
        raise ValueError(f"steps[{idx}] needs left/right or linear/angular")
      parsed.append((command, duration))
    self.steps = parsed
    self.topic = topic
    self.loop = loop
    self.start_delay_ns = int(start_delay_s * 1e9)
    self.total_ns = int(sum(d for _, d in parsed) * 1e9)
    self._start_ns: int | None = None
    self._index = -1

  def setup(self, ctx: NodeContext) -> None:
    self._start_ns = ctx.now_ns

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    if self._start_ns is None:
      self._start_ns = ctx.now_ns
    elapsed = ctx.now_ns - self._start_ns - self.start_delay_ns
    if elapsed < 0:
      return None
    if elapsed >= self.total_ns:
      if not self.loop:
        self._index = -1
        return None
      elapsed %= self.total_ns
    for idx, (command, duration) in enumerate(self.steps):
      span = int(duration * 1e9)
      if elapsed < span:
        self._index = idx
        return [(self.topic, "DriveCommand", {**command, "step": idx})]
      elapsed -= span
    return None

  def status(self) -> dict[str, Any]:
    return {"step": self._index, "steps": len(self.steps), "loop": self.loop}


class SkidSteerSim(LiveNode):
  """First-order kinematic model of a skid-steer chassis driven by `drive.state` duty.

  Wheel speed follows duty with a static-friction threshold and a time constant; body speed
  and yaw rate come from the side speeds and track width. Publishes pose and velocity on
  `odom_topic`.

  With `world`, the robot (a circle of `robot_radius_m`) starts at `start` ([x, y,
  heading_deg], default the arena centre facing +x), is stopped by walls and obstacles
  (each new contact counts as a collision), and `range_sensors` publish
  {"distance_m", "max_range_m", "hit"} on `range.<name>` every `range_every_n_ticks`.
  `lidar` publishes 360-degree scans in the `lidar` node's format.
  """

  def __init__(
    self,
    *,
    state_topic: str = "drive.state",
    odom_topic: str = "odom",
    max_speed_mps: float = 0.6,
    track_width_m: float = 0.25,
    time_constant_s: float = 0.15,
    stiction_duty: float = 0.15,
    odom_every_n_ticks: int = 5,
    world: dict[str, Any] | None = None,
    robot_radius_m: float = 0.12,
    start: list[float] | None = None,
    range_sensors: list[dict[str, Any]] | None = None,
    range_every_n_ticks: int = 2,
    lidar: dict[str, Any] | None = None,
    seed: int = 0,
  ) -> None:
    if max_speed_mps <= 0 or track_width_m <= 0 or time_constant_s <= 0:
      raise ValueError("max_speed_mps, track_width_m, and time_constant_s must be > 0")
    if not 0.0 <= stiction_duty < 1.0:
      raise ValueError("stiction_duty must be in [0, 1)")
    if robot_radius_m <= 0:
      raise ValueError("robot_radius_m must be > 0")
    self.world = SimWorld.from_dict(world) if world is not None else None
    self.sensors = [RangeSensorSpec.parse(raw, idx) for idx, raw in enumerate(range_sensors or [])]
    if self.sensors and self.world is None:
      raise ValueError("range_sensors need a world to see")
    if len({s.name for s in self.sensors}) != len(self.sensors):
      raise ValueError("range_sensors names must be unique")
    self.lidar = self._parse_lidar(lidar)
    self.input_topics = (state_topic,)
    self.state_topic = state_topic
    self.odom_topic = odom_topic
    self.max_speed_mps = max_speed_mps
    self.track_width_m = track_width_m
    self.time_constant_s = time_constant_s
    self.stiction_duty = stiction_duty
    self.odom_every_n_ticks = max(1, int(odom_every_n_ticks))
    self.range_every_n_ticks = max(1, int(range_every_n_ticks))
    self.robot_radius_m = float(robot_radius_m)
    self.duty = (0.0, 0.0)
    self.wheel_speed = [0.0, 0.0]
    self.x_m = 0.0
    self.y_m = 0.0
    self.heading_rad = 0.0
    if self.world is not None:
      x, y, heading_deg = (
        _numbers(start, 3, "start") if start is not None else [self.world.width_m / 2, self.world.height_m / 2, 0.0]
      )
      if self.world.collision(x, y, self.robot_radius_m):
        raise ValueError(f"start [{x}, {y}] puts the robot inside {self.world.collision(x, y, self.robot_radius_m)}")
      self.x_m, self.y_m, self.heading_rad = x, y, math.radians(heading_deg)
    elif start is not None:
      raise ValueError("start needs a world")
    self.distance_m = 0.0
    self.collisions = 0
    self.bumped: str | None = None
    self.ranges: dict[str, float] = {}
    self._rng = random.Random(seed)
    self._free_m = 0.0
    self._collisions: Any = None
    self._last_ns: int | None = None
    self._ticks = 0
    self._period_ns = 0
    self._gauges: dict[str, Any] = {}

  def _parse_lidar(self, raw: Any) -> dict[str, Any] | None:
    if raw is None:
      return None
    if not isinstance(raw, dict):
      raise ValueError("lidar must be a mapping, e.g. {range_max_m: 8.0}")
    if self.world is None:
      raise ValueError("lidar needs a world to see")
    unknown = sorted(set(raw) - _LIDAR_KEYS)
    if unknown:
      raise ValueError(f"lidar: unknown fields {', '.join(unknown)}")
    spec = {"topic": "scan", "bins": 360, "range_min_m": 0.05, "range_max_m": 8.0, "noise_m": 0.0, "every_n_ticks": 5, **raw}
    try:
      spec.update(
        bins=int(spec["bins"]),
        every_n_ticks=max(1, int(spec["every_n_ticks"])),
        range_min_m=float(spec["range_min_m"]),
        range_max_m=float(spec["range_max_m"]),
        noise_m=float(spec["noise_m"]),
      )
    except (TypeError, ValueError):
      raise ValueError("lidar: bins, every_n_ticks, range_min_m, range_max_m, and noise_m must be numbers") from None
    if not 36 <= spec["bins"] <= 1440 or not 0 <= spec["range_min_m"] < spec["range_max_m"] or spec["noise_m"] < 0:
      raise ValueError("lidar: need bins 36..1440, 0 <= range_min_m < range_max_m, and noise_m >= 0")
    return spec

  def _scan(self) -> Output:
    assert self.world is not None and self.lidar is not None
    spec = self.lidar
    ranges = ray_scan(self.world, self.x_m, self.y_m, self.heading_rad, bins=spec["bins"], range_max_m=spec["range_max_m"])
    points = 0
    for i, distance in enumerate(ranges):
      if distance is None:
        continue
      if spec["noise_m"]:
        distance += self._rng.gauss(0.0, spec["noise_m"])
      if spec["range_min_m"] <= distance <= spec["range_max_m"]:
        ranges[i] = round(distance, 3)
        points += 1
      else:
        ranges[i] = None
    rate = 1e9 / (self._period_ns * spec["every_n_ticks"]) if self._period_ns else 0.0
    payload = scan_payload(ranges, range_min_m=spec["range_min_m"], range_max_m=spec["range_max_m"], points=points, scan_hz=rate, model="sim")
    # Where the scan was taken from, so a viewer can draw it on the map without lag.
    payload["pose"] = {"x_m": round(self.x_m, 4), "y_m": round(self.y_m, 4), "heading_deg": round(math.degrees(self.heading_rad), 3)}
    return (spec["topic"], "LaserScan", payload)

  def setup(self, ctx: NodeContext) -> None:
    rate = getattr(ctx, "rate_hz", None)
    self._period_ns = int(1e9 / rate) if rate else 0
    for name in ("speed_mps", "yaw_rate_dps", "heading_deg", "distance_m"):
      self._gauges[name] = ctx.metrics.gauge(f"nervlynx_sim_{name}")
    if self.world is not None:
      self._collisions = ctx.metrics.counter("nervlynx_sim_collisions_total")
      ctx.metrics.describe("nervlynx_sim_collisions_total", "Times the simulated robot ran into a wall or obstacle.")

  def on_message(self, msg: RuntimeMessage, ctx: NodeContext) -> Iterable[Output] | None:
    p = msg.payload
    if "left_applied" in p and "right_applied" in p:
      self.duty = (float(p["left_applied"]), float(p["right_applied"]))
    return None

  def _wheel_target(self, duty: float, moving: bool) -> float:
    magnitude = abs(duty)
    # A stationary wheel needs to overcome static friction; a rolling one keeps turning.
    threshold = self.stiction_duty if not moving else self.stiction_duty * 0.5
    if magnitude <= threshold:
      return 0.0
    speed = self.max_speed_mps * (magnitude - threshold) / (1.0 - threshold)
    return math.copysign(speed, duty)

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    now = ctx.now_ns
    dt = 0.0 if self._last_ns is None else (now - self._last_ns) / 1e9
    self._last_ns = now
    self._ticks += 1
    if dt > 0:
      alpha = 1.0 - math.exp(-dt / self.time_constant_s)
      for idx in (0, 1):
        target = self._wheel_target(self.duty[idx], abs(self.wheel_speed[idx]) > 1e-3)
        self.wheel_speed[idx] += (target - self.wheel_speed[idx]) * alpha
      v_left, v_right = self.wheel_speed
      speed = (v_left + v_right) / 2.0
      yaw_rate = (v_right - v_left) / self.track_width_m
      self.heading_rad = (self.heading_rad + yaw_rate * dt + math.pi) % (2 * math.pi) - math.pi
      new_x = self.x_m + speed * math.cos(self.heading_rad) * dt
      new_y = self.y_m + speed * math.sin(self.heading_rad) * dt
      hit = self.world.collision(new_x, new_y, self.robot_radius_m) if self.world is not None else None
      if hit is None:
        travel = abs(speed) * dt
        self.x_m, self.y_m = new_x, new_y
        self.distance_m += travel
        if self.bumped is not None:
          self._free_m += travel
          if self._free_m >= _CONTACT_RELEASE_M:
            self.bumped = None
      else:
        # Advance to the contact point, then stall against it. Stopping short would leave a
        # gap the robot creeps into, making one contact look like many.
        lo, hi = 0.0, 1.0
        for _ in range(12):
          mid = (lo + hi) / 2
          probe_x = self.x_m + (new_x - self.x_m) * mid
          probe_y = self.y_m + (new_y - self.y_m) * mid
          if self.world.collision(probe_x, probe_y, self.robot_radius_m) is None:
            lo = mid
          else:
            hi = mid
        contact_x = self.x_m + (new_x - self.x_m) * lo
        contact_y = self.y_m + (new_y - self.y_m) * lo
        self.distance_m += math.hypot(contact_x - self.x_m, contact_y - self.y_m)
        self.x_m, self.y_m = contact_x, contact_y
        self.wheel_speed = [0.0, 0.0]
        self._free_m = 0.0
        if self.bumped is None:
          self.collisions += 1
          if self._collisions is not None:
            self._collisions.inc()
          ctx.fault(f"sim: robot ran into {hit} at ({self.x_m:.2f}, {self.y_m:.2f})", kind="sim_collision")
        self.bumped = hit
    speed, yaw_rate_dps = self.body_velocity()
    if self._gauges:
      self._gauges["speed_mps"].set(round(speed, 4))
      self._gauges["yaw_rate_dps"].set(round(yaw_rate_dps, 3))
      self._gauges["heading_deg"].set(round(math.degrees(self.heading_rad), 3))
      self._gauges["distance_m"].set(round(self.distance_m, 4))
    out: list[Output] = []
    if self.sensors and self._ticks % self.range_every_n_ticks == 0:
      out.extend(self._read_ranges())
    if self.lidar is not None and self._ticks % self.lidar["every_n_ticks"] == 0:
      out.append(self._scan())
    if self._ticks % self.odom_every_n_ticks == 0:
      out.append((self.odom_topic, "Odometry", self.odometry()))
    return out or None

  def _read_ranges(self) -> list[Output]:
    assert self.world is not None
    readings: list[Output] = []
    for sensor in self.sensors:
      mount = self.heading_rad + sensor.angle_rad
      ox = self.x_m + self.robot_radius_m * math.cos(mount)
      oy = self.y_m + self.robot_radius_m * math.sin(mount)
      true_m = min(self.world.ray(ox, oy, self.heading_rad + angle, math.inf) for angle in sensor.ray_angles())
      measured = true_m + (self._rng.gauss(0.0, sensor.noise_m) if sensor.noise_m else 0.0)
      distance = min(max(measured, 0.0), sensor.max_range_m)
      self.ranges[sensor.name] = round(distance, 4)
      readings.append(
        (
          sensor.topic,
          "Range",
          {"distance_m": round(distance, 4), "max_range_m": sensor.max_range_m, "hit": true_m <= sensor.max_range_m},
        )
      )
    return readings

  def body_velocity(self) -> tuple[float, float]:
    v_left, v_right = self.wheel_speed
    return (v_left + v_right) / 2.0, math.degrees((v_right - v_left) / self.track_width_m)

  def odometry(self) -> dict[str, Any]:
    speed, yaw_rate_dps = self.body_velocity()
    return {
      "x_m": round(self.x_m, 4),
      "y_m": round(self.y_m, 4),
      "heading_deg": round(math.degrees(self.heading_rad), 3),
      "speed_mps": round(speed, 4),
      "yaw_rate_dps": round(yaw_rate_dps, 3),
      "distance_m": round(self.distance_m, 4),
    }

  def status(self) -> dict[str, Any]:
    status = self.odometry()
    if self.world is not None:
      status.update(
        {
          "collisions": self.collisions,
          "bumped": self.bumped,
          "ranges": dict(self.ranges),
          "sensors": [
            {
              "name": s.name,
              "angle_deg": round(math.degrees(s.angle_rad), 3),
              "beam_deg": round(math.degrees(s.beam_rad), 3),
              "max_range_m": s.max_range_m,
            }
            for s in self.sensors
          ],
          "robot_radius_m": self.robot_radius_m,
          "world": self.world.to_dict(),
        }
      )
      if self.lidar is not None:
        status["lidar"] = {"topic": self.lidar["topic"], "range_max_m": self.lidar["range_max_m"]}
    return status
