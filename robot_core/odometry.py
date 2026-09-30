"""`wheel_odometry`: where the robot is, from its wheel encoders (differential drive).

Reads cumulative encoder counts ({"left_ticks", "right_ticks"}, as the `esp32_link` node
publishes on `link.encoders`) and publishes pose and speed on `odom` in the same format
as the simulator, so the ROS 2 bridge, skills, and your own nodes work the same on the
robot and in simulation:

  {"x_m", "y_m", "heading_deg", "speed_mps", "yaw_rate_dps", "distance_m"}

x is forward from where the robot started and heading is counter-clockwise, in degrees.
Wheel odometry drifts, most of all in heading on carpet and when wheels slip; it is good
for metres, not for a whole building.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from robot_core.live import LiveNode, NodeContext, Output
from robot_core.runtime import RuntimeMessage


class WheelOdometry(LiveNode):
  """Integrates left/right encoder counts into a pose."""

  def __init__(
    self,
    *,
    encoder_topic: str = "link.encoders",
    odom_topic: str = "odom",
    ticks_per_meter: float | None = None,
    wheel_diameter_m: float | None = None,
    ticks_per_rev: float | None = None,
    track_width_m: float = 0.25,
    invert_left: bool = False,
    invert_right: bool = False,
  ) -> None:
    """Give `ticks_per_meter`, or `wheel_diameter_m` and `ticks_per_rev` (counts per wheel
    turn, after any gearbox). Measure it: drive a metre in a straight line and divide."""
    if ticks_per_meter is None:
      if not wheel_diameter_m or not ticks_per_rev:
        raise ValueError("give ticks_per_meter, or wheel_diameter_m and ticks_per_rev")
      ticks_per_meter = ticks_per_rev / (math.pi * wheel_diameter_m)
    if ticks_per_meter <= 0 or track_width_m <= 0:
      raise ValueError("ticks_per_meter and track_width_m must be > 0")
    self.input_topics = (encoder_topic,)
    self.odom_topic = odom_topic
    self.ticks_per_meter = float(ticks_per_meter)
    self.track_width_m = float(track_width_m)
    self.signs = (-1.0 if invert_left else 1.0, -1.0 if invert_right else 1.0)
    self.x_m = self.y_m = self.heading_rad = self.distance_m = 0.0
    self.speed_mps = self.yaw_rate_dps = 0.0
    self._last: tuple[float, float, int] | None = None

  def on_message(self, msg: RuntimeMessage, ctx: NodeContext) -> Iterable[Output] | None:
    try:
      left = float(msg.payload["left_ticks"]) * self.signs[0]
      right = float(msg.payload["right_ticks"]) * self.signs[1]
    except (KeyError, TypeError, ValueError):
      ctx.fault(f"wheel_odometry: encoder message without left_ticks/right_ticks: {msg.payload!r}", kind="bad_message")
      return None
    now = ctx.now_ns
    if self._last is None:
      self._last = (left, right, now)
      return [(self.odom_topic, "Odometry", self.odometry())]
    last_left, last_right, last_ns = self._last
    self._last = (left, right, now)
    d_left = (left - last_left) / self.ticks_per_meter
    d_right = (right - last_right) / self.ticks_per_meter
    d_center = (d_left + d_right) / 2.0
    d_theta = (d_right - d_left) / self.track_width_m
    mid = self.heading_rad + d_theta / 2.0
    self.x_m += d_center * math.cos(mid)
    self.y_m += d_center * math.sin(mid)
    self.heading_rad = (self.heading_rad + d_theta + math.pi) % (2 * math.pi) - math.pi
    self.distance_m += abs(d_center)
    dt = (now - last_ns) / 1e9
    if dt > 0:
      self.speed_mps = d_center / dt
      self.yaw_rate_dps = math.degrees(d_theta / dt)
    return [(self.odom_topic, "Odometry", self.odometry())]

  def odometry(self) -> dict[str, Any]:
    return {
      "x_m": round(self.x_m, 4),
      "y_m": round(self.y_m, 4),
      "heading_deg": round(math.degrees(self.heading_rad), 3),
      "speed_mps": round(self.speed_mps, 4),
      "yaw_rate_dps": round(self.yaw_rate_dps, 3),
      "distance_m": round(self.distance_m, 4),
    }

  def status(self) -> dict[str, Any]:
    return self.odometry()
