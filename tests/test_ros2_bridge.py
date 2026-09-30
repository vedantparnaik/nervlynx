import math
import sys
import threading
import types

import pytest

from robot_core.live import LiveRuntime
from robot_core.live_config import validate_live_config
from robot_core.project import build_registry
from robot_core.ros2_bridge import (
  Ros2Bridge,
  gps_to_ros,
  imu_to_ros,
  odometry_to_ros,
  range_to_ros,
  scan_to_ros,
  twist_to_drive,
)
from robot_core.runtime import SimulatedClock


def test_odometry_becomes_a_ros_pose_and_twist() -> None:
  ros = odometry_to_ros({"x_m": 1.5, "y_m": -0.5, "heading_deg": 90.0, "speed_mps": 0.3, "yaw_rate_dps": 45.0})
  assert ros["pose"]["pose"]["position"] == {"x": 1.5, "y": -0.5, "z": 0.0}
  q = ros["pose"]["pose"]["orientation"]
  assert (q["z"], q["w"]) == (pytest.approx(math.sqrt(0.5)), pytest.approx(math.sqrt(0.5)))
  assert ros["twist"]["twist"]["angular"]["z"] == pytest.approx(math.pi / 4) and ros["child_frame_id"] == "base_link"


def test_scans_imu_ranges_and_fixes_follow_ros_conventions() -> None:
  scan = scan_to_ros({"ranges_m": [1.0, None, 2.0, None], "angle_increment_deg": 90.0, "range_min_m": 0.05, "range_max_m": 12.0, "scan_hz": 10.0})
  assert scan["ranges"] == [1.0, math.inf, 2.0, math.inf]
  assert scan["angle_max"] == pytest.approx(3 * math.pi / 2) and scan["scan_time"] == pytest.approx(0.1)
  imu = imu_to_ros({"accel_mps2": [0.0, 0.0, 9.8], "gyro_dps": [0.0, 0.0, 180.0]})
  assert imu["angular_velocity"]["z"] == pytest.approx(math.pi) and imu["orientation_covariance"][0] == -1.0
  assert range_to_ros({"distance_m": 2.0, "max_range_m": 2.0, "hit": False})["range"] == math.inf
  assert range_to_ros({"distance_m": 0.4, "max_range_m": 2.0, "hit": True})["range"] == 0.4
  fix = gps_to_ros({"fix": True, "lat_deg": 51.5, "lon_deg": -0.1, "alt_m": 10.0, "hdop": 1.0})
  assert fix["status"]["status"] == 0 and fix["position_covariance"][0] == 25.0 and fix["position_covariance_type"] == 1
  none = gps_to_ros({"fix": False, "hdop": None})
  assert none["status"]["status"] == -1 and math.isnan(none["latitude"]) and none["position_covariance_type"] == 0


def test_twist_is_scaled_to_the_robot_and_clamped() -> None:
  assert twist_to_drive({"linear": {"x": 0.25}, "angular": {"z": math.radians(45)}}, max_linear_mps=0.5, max_angular_dps=90) == {"linear": 0.5, "angular": 0.5}
  assert twist_to_drive({"linear": {"x": 5.0}, "angular": {"z": -10.0}}, max_linear_mps=0.5, max_angular_dps=90) == {"linear": 1.0, "angular": -1.0}
  assert twist_to_drive({"linear": {"x": math.nan}}, max_linear_mps=0.5, max_angular_dps=90) == {"linear": 0.0, "angular": 0.0}


def test_links_are_validated() -> None:
  with pytest.raises(ValueError, match=r"to_ros\[0\].type must be one of"):
    Ros2Bridge(to_ros=[{"topic": "odom", "ros_topic": "/odom", "type": "pose"}])
  with pytest.raises(ValueError, match="compressed_image needs camera"):
    Ros2Bridge(to_ros=[{"ros_topic": "/image", "type": "compressed_image"}])
  bridge = Ros2Bridge()
  assert bridge.input_topics == ("odom", "scan", "imu", "gps", "safety.estop")
  reg = build_registry()
  assert validate_live_config({"nodes": [{"plugin": "ros2_bridge"}]}, reg) == []


class Msg:
  """Stand-in for a ROS message: nested fields appear when first used."""

  def __getattr__(self, name):
    if name.startswith("__"):
      raise AttributeError(name)
    value = Msg()
    object.__setattr__(self, name, value)
    return value


class FakeRos:
  def __init__(self):
    self.inited = False
    self.publishers: dict[str, list] = {}
    self.subscriptions: dict[str, object] = {}
    self.transforms: list = []
    self.destroyed = False

  def install(self, monkeypatch) -> None:
    ros = self
    stop = threading.Event()

    class Node:
      def __init__(self, name, namespace=None):
        self.name = name

      def create_publisher(self, msg_type, topic, qos):
        sent = ros.publishers.setdefault(topic, [])
        return types.SimpleNamespace(publish=sent.append)

      def create_subscription(self, msg_type, topic, callback, qos):
        ros.subscriptions[topic] = callback

      def get_clock(self):
        return types.SimpleNamespace(now=lambda: types.SimpleNamespace(to_msg=lambda: "stamp"))

      def destroy_node(self):
        ros.destroyed = True

    class Executor:
      def add_node(self, node):
        pass

      def spin(self):
        stop.wait(5)

      def shutdown(self):
        stop.set()

    rclpy = types.ModuleType("rclpy")
    rclpy.ok = lambda: ros.inited
    rclpy.init = lambda args=None: setattr(ros, "inited", True)
    rclpy.shutdown = lambda: setattr(ros, "inited", False)
    rclpy.create_node = Node
    modules = {
      "rclpy": rclpy,
      "rclpy.executors": types.SimpleNamespace(SingleThreadedExecutor=Executor),
      "rclpy.qos": types.SimpleNamespace(QoSProfile=lambda depth: ("reliable", depth), qos_profile_sensor_data=("sensor", 5)),
      "tf2_ros": types.SimpleNamespace(TransformBroadcaster=lambda node: types.SimpleNamespace(sendTransform=ros.transforms.append)),
    }
    for package, names in {
      "geometry_msgs": ("Twist", "TransformStamped"),
      "nav_msgs": ("Odometry",),
      "sensor_msgs": ("CompressedImage", "Imu", "LaserScan", "NavSatFix", "Range"),
      "std_msgs": ("Bool", "String"),
    }.items():
      modules[package] = types.ModuleType(package)
      modules[f"{package}.msg"] = types.SimpleNamespace(**{name: type(name, (Msg,), {}) for name in names})
    for name, module in modules.items():
      monkeypatch.setitem(sys.modules, name, module)


def bridge_runtime(bridge: Ros2Bridge) -> LiveRuntime:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("ros", bridge)
  return rt


def step(rt: LiveRuntime, n: int = 1) -> None:
  for _ in range(n):
    rt.clock.advance_ms(20)
    rt.step()


def test_the_bridge_publishes_to_ros_and_turns_cmd_vel_into_drive_commands(monkeypatch) -> None:
  ros = FakeRos()
  ros.install(monkeypatch)
  bridge = Ros2Bridge(max_linear_mps=0.4, max_angular_dps=60, from_ros=[
    {"ros_topic": "/cmd_vel", "topic": "cmd.drive", "type": "twist"},
    {"ros_topic": "/nervlynx/say", "topic": "agent.command", "type": "json"},
  ])
  rt = bridge_runtime(bridge)
  drive: list = []
  commands: list = []
  rt.subscribe("probe", "cmd.drive", drive.append)
  rt.subscribe("probe2", "agent.command", commands.append)
  rt.start()
  rt.publish_external("odom", "Odometry", {"x_m": 1.0, "y_m": 2.0, "heading_deg": 0.0, "speed_mps": 0.2, "yaw_rate_dps": 0.0})
  rt.publish_external("scan", "LaserScan", {"ranges_m": [1.0, None], "angle_increment_deg": 180.0, "range_min_m": 0.05, "range_max_m": 8.0})
  step(rt)
  odom = ros.publishers["/odom"][-1]
  assert odom.pose.pose.position.x == 1.0 and odom.header.frame_id == "odom" and odom.header.stamp == "stamp"
  assert ros.transforms[-1].transform.translation.y == 2.0 and ros.transforms[-1].child_frame_id == "base_link"
  assert ros.publishers["/scan"][-1].ranges == [1.0, math.inf]
  rt.request_estop("test", source="test")
  step(rt)
  assert ros.publishers["/nervlynx/estop"][-1].data is True
  for vx in (0.1, 0.2):  # only the newest velocity is forwarded
    ros.subscriptions["/cmd_vel"](types.SimpleNamespace(linear=types.SimpleNamespace(x=vx), angular=types.SimpleNamespace(z=math.radians(30))))
  ros.subscriptions["/nervlynx/say"](types.SimpleNamespace(data='{"text": "hello"}'))
  ros.subscriptions["/nervlynx/say"](types.SimpleNamespace(data="not json"))
  step(rt, 2)
  assert [m.payload for m in drive] == [{"linear": 0.5, "angular": 0.5}]
  assert [m.payload for m in commands] == [{"text": "hello"}]
  assert bridge.status()["errors"] == 1 and bridge.status()["from_ros"] == {"/cmd_vel": "cmd.drive", "/nervlynx/say": "agent.command"}
  rt.shutdown()
  assert ros.destroyed and ros.inited is False


def test_without_ros_the_bridge_explains_what_to_source(monkeypatch) -> None:
  monkeypatch.setitem(sys.modules, "rclpy", None)
  with pytest.raises(ModuleNotFoundError, match="source /opt/ros/<distro>/setup.bash"):
    Ros2Bridge().setup(None)
