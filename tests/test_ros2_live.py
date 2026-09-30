"""Runs only where ROS 2 is installed (CI runs it in a ros:humble container)."""

import math
import threading
import time

import pytest

rclpy = pytest.importorskip("rclpy")

from geometry_msgs.msg import Twist  # noqa: E402
from nav_msgs.msg import Odometry  # noqa: E402
from rclpy.executors import SingleThreadedExecutor  # noqa: E402
from sensor_msgs.msg import LaserScan  # noqa: E402

from robot_core.live import LiveRuntime  # noqa: E402
from robot_core.ros2_bridge import Ros2Bridge  # noqa: E402


def test_real_ros2_round_trip() -> None:
  rclpy.init()
  try:
    probe = rclpy.create_node("nervlynx_probe")
    odoms: list = []
    scans: list = []
    probe.create_subscription(Odometry, "/odom", odoms.append, 10)
    probe.create_subscription(LaserScan, "/scan", scans.append, rclpy.qos.qos_profile_sensor_data)
    cmd_vel = probe.create_publisher(Twist, "/cmd_vel", 10)
    executor = SingleThreadedExecutor()
    executor.add_node(probe)
    spinner = threading.Thread(target=executor.spin, daemon=True)
    spinner.start()

    rt = LiveRuntime(seed=1)
    rt.add_node("ros", Ros2Bridge(max_linear_mps=0.5))
    drive: list = []
    rt.subscribe("probe", "cmd.drive", drive.append)
    rt.start()
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and not (odoms and scans and drive):
      rt.publish_external("odom", "Odometry", {"x_m": 1.0, "y_m": 2.0, "heading_deg": 90.0, "speed_mps": 0.2, "yaw_rate_dps": 0.0})
      rt.publish_external("scan", "LaserScan", {"ranges_m": [1.0, None, 2.0, None], "angle_increment_deg": 90.0, "range_min_m": 0.05, "range_max_m": 8.0, "scan_hz": 10.0})
      twist = Twist()
      twist.linear.x = 0.25
      cmd_vel.publish(twist)
      rt.step()
      time.sleep(0.05)
    rt.shutdown()
    executor.shutdown()
    assert odoms and odoms[-1].pose.pose.position.y == 2.0
    assert odoms[-1].pose.pose.orientation.z == pytest.approx(math.sqrt(0.5), abs=1e-6)
    assert scans and math.isinf(scans[-1].ranges[1])
    assert drive and drive[-1].payload == {"linear": 0.5, "angular": 0.0}
  finally:
    if rclpy.ok():
      rclpy.shutdown()
