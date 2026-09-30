"""Simulation nodes: scripted command sources and a skid-steer plant model.

They let a full sense -> command -> actuate -> motion loop run on a laptop or in CI with
the mock hardware backend, and give soak tests a repeatable workload.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from robot_core.live import LiveNode, NodeContext, Output
from robot_core.runtime import RuntimeMessage


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
  ) -> None:
    if max_speed_mps <= 0 or track_width_m <= 0 or time_constant_s <= 0:
      raise ValueError("max_speed_mps, track_width_m, and time_constant_s must be > 0")
    if not 0.0 <= stiction_duty < 1.0:
      raise ValueError("stiction_duty must be in [0, 1)")
    self.input_topics = (state_topic,)
    self.state_topic = state_topic
    self.odom_topic = odom_topic
    self.max_speed_mps = max_speed_mps
    self.track_width_m = track_width_m
    self.time_constant_s = time_constant_s
    self.stiction_duty = stiction_duty
    self.odom_every_n_ticks = max(1, int(odom_every_n_ticks))
    self.duty = (0.0, 0.0)
    self.wheel_speed = [0.0, 0.0]
    self.x_m = 0.0
    self.y_m = 0.0
    self.heading_rad = 0.0
    self.distance_m = 0.0
    self._last_ns: int | None = None
    self._ticks = 0
    self._gauges: dict[str, Any] = {}

  def setup(self, ctx: NodeContext) -> None:
    for name in ("speed_mps", "yaw_rate_dps", "heading_deg", "distance_m"):
      self._gauges[name] = ctx.metrics.gauge(f"nervlynx_sim_{name}")

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
      self.x_m += speed * math.cos(self.heading_rad) * dt
      self.y_m += speed * math.sin(self.heading_rad) * dt
      self.distance_m += abs(speed) * dt
    speed, yaw_rate_dps = self.body_velocity()
    if self._gauges:
      self._gauges["speed_mps"].set(round(speed, 4))
      self._gauges["yaw_rate_dps"].set(round(yaw_rate_dps, 3))
      self._gauges["heading_deg"].set(round(math.degrees(self.heading_rad), 3))
      self._gauges["distance_m"].set(round(self.distance_m, 4))
    if self._ticks % self.odom_every_n_ticks:
      return None
    return [(self.odom_topic, "Odometry", self.odometry())]

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
    return self.odometry()
