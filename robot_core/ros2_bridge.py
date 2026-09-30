"""`ros2_bridge`: share topics with ROS 2 so slam_toolbox, Nav2, RViz, and rosbag work.

NervLynx stays the lightweight layer on the robot (drivers, safety, the dashboard), and
ROS 2 runs where it fits, usually an Orin NX or a laptop. The bridge node runs in the
NervLynx process on the machine that has ROS 2 (source its setup.bash first; rclpy comes
with ROS 2, not from pip) and converts messages both ways:

  to ROS      odom -> nav_msgs/Odometry (+ the odom->base_link transform), scan ->
              sensor_msgs/LaserScan, imu -> sensor_msgs/Imu, range.<name> -> sensor_msgs/Range,
              gps -> sensor_msgs/NavSatFix, a camera -> sensor_msgs/CompressedImage,
              safety.estop -> std_msgs/Bool, anything else -> std_msgs/String (JSON)
  from ROS    geometry_msgs/Twist (Nav2's /cmd_vel) -> cmd.drive, scaled by the robot's
              top speed and clamped; std_msgs/String (JSON) -> any topic

Commands from ROS go through the same drive node as everything else, so the deadman
stops the robot when Nav2 stops publishing and the e-stop always wins. With the mesh,
the bridge can run on the Orin while the drivers stay on the Pi.
"""

from __future__ import annotations

import json
import math
import threading
from collections import deque
from typing import Any, Iterable

from robot_core.live import ESTOP_TOPIC, LiveNode, NodeContext, Output
from robot_core.runtime import RuntimeMessage

TO_ROS_TYPES = ("odometry", "laser_scan", "imu", "range", "nav_sat_fix", "compressed_image", "bool", "json")
FROM_ROS_TYPES = ("twist", "json")
DEFAULT_TO_ROS = [
  {"topic": "odom", "ros_topic": "/odom", "type": "odometry", "tf": True},
  {"topic": "scan", "ros_topic": "/scan", "type": "laser_scan"},
  {"topic": "imu", "ros_topic": "/imu/data_raw", "type": "imu"},
  {"topic": "gps", "ros_topic": "/fix", "type": "nav_sat_fix"},
  {"topic": ESTOP_TOPIC, "ros_topic": "/nervlynx/estop", "type": "bool"},
]
DEFAULT_FROM_ROS = [{"ros_topic": "/cmd_vel", "topic": "cmd.drive", "type": "twist"}]
_FRAME_IDS = {"odometry": "odom", "laser_scan": "laser", "imu": "imu_link", "range": "range", "nav_sat_fix": "gps", "compressed_image": "camera"}
_ROS_HINT = "the ros2_bridge needs ROS 2: source /opt/ros/<distro>/setup.bash (rclpy ships with ROS 2), then run nervlynx again"


def yaw_quaternion(yaw_rad: float) -> dict[str, float]:
  return {"x": 0.0, "y": 0.0, "z": math.sin(yaw_rad / 2), "w": math.cos(yaw_rad / 2)}


def odometry_to_ros(p: dict[str, Any], *, frame_id: str = "odom", child_frame_id: str = "base_link") -> dict[str, Any]:
  yaw = math.radians(float(p.get("heading_deg", 0.0)))
  return {
    "header": {"frame_id": frame_id},
    "child_frame_id": child_frame_id,
    "pose": {"pose": {"position": {"x": float(p.get("x_m", 0.0)), "y": float(p.get("y_m", 0.0)), "z": 0.0}, "orientation": yaw_quaternion(yaw)}},
    "twist": {"twist": {"linear": {"x": float(p.get("speed_mps", 0.0)), "y": 0.0, "z": 0.0}, "angular": {"x": 0.0, "y": 0.0, "z": math.radians(float(p.get("yaw_rate_dps", 0.0)))}}},
  }


def odometry_transform(p: dict[str, Any], *, frame_id: str = "odom", child_frame_id: str = "base_link") -> dict[str, Any]:
  return {
    "header": {"frame_id": frame_id},
    "child_frame_id": child_frame_id,
    "transform": {
      "translation": {"x": float(p.get("x_m", 0.0)), "y": float(p.get("y_m", 0.0)), "z": 0.0},
      "rotation": yaw_quaternion(math.radians(float(p.get("heading_deg", 0.0)))),
    },
  }


def scan_to_ros(p: dict[str, Any], *, frame_id: str = "laser") -> dict[str, Any]:
  ranges = p.get("ranges_m") or []
  increment = math.radians(float(p.get("angle_increment_deg") or (360.0 / max(1, len(ranges)))))
  start = math.radians(float(p.get("angle_min_deg", 0.0)))
  hz = float(p.get("scan_hz") or 0.0)
  return {
    "header": {"frame_id": frame_id},
    "angle_min": start,
    "angle_max": start + increment * (len(ranges) - 1),
    "angle_increment": increment,
    "time_increment": 0.0,
    "scan_time": 1.0 / hz if hz > 0 else 0.0,
    "range_min": float(p.get("range_min_m", 0.0)),
    "range_max": float(p.get("range_max_m", 0.0)),
    "ranges": [math.inf if d is None else float(d) for d in ranges],  # REP-117: +inf is "no return"
    "intensities": [],
  }


def imu_to_ros(p: dict[str, Any], *, frame_id: str = "imu_link") -> dict[str, Any]:
  accel = p.get("accel_mps2") or [0.0, 0.0, 0.0]
  gyro = p.get("gyro_dps") or [0.0, 0.0, 0.0]
  return {
    "header": {"frame_id": frame_id},
    "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
    "orientation_covariance": [-1.0] + [0.0] * 8,  # no orientation estimate
    "angular_velocity": dict(zip("xyz", (math.radians(float(v)) for v in gyro))),
    "linear_acceleration": dict(zip("xyz", (float(v) for v in accel))),
  }


def range_to_ros(p: dict[str, Any], *, frame_id: str = "range", field_of_view_deg: float = 30.0, infrared: bool = False) -> dict[str, Any]:
  max_range = float(p.get("max_range_m", 0.0))
  distance = float(p.get("distance_m", max_range))
  return {
    "header": {"frame_id": frame_id},
    "radiation_type": 1 if infrared else 0,  # ULTRASOUND=0, INFRARED=1
    "field_of_view": math.radians(field_of_view_deg),
    "min_range": 0.02,
    "max_range": max_range,
    "range": distance if p.get("hit", True) else math.inf,
  }


def gps_to_ros(p: dict[str, Any], *, frame_id: str = "gps") -> dict[str, Any]:
  fix = bool(p.get("fix"))
  hdop = p.get("hdop")
  covariance, kind = [0.0] * 9, 0
  if fix and isinstance(hdop, (int, float)):
    horizontal, vertical = (hdop * 5.0) ** 2, (hdop * 10.0) ** 2  # ~5 m per unit of HDOP for a hobby receiver
    covariance, kind = [horizontal, 0.0, 0.0, 0.0, horizontal, 0.0, 0.0, 0.0, vertical], 1  # APPROXIMATED
  return {
    "header": {"frame_id": frame_id},
    "status": {"status": 0 if fix else -1, "service": 1},  # STATUS_FIX / STATUS_NO_FIX, SERVICE_GPS
    "latitude": float(p["lat_deg"]) if fix and p.get("lat_deg") is not None else math.nan,
    "longitude": float(p["lon_deg"]) if fix and p.get("lon_deg") is not None else math.nan,
    "altitude": float(p["alt_m"]) if fix and p.get("alt_m") is not None else math.nan,
    "position_covariance": covariance,
    "position_covariance_type": kind,
  }


def twist_to_drive(twist: dict[str, Any], *, max_linear_mps: float, max_angular_dps: float) -> dict[str, float]:
  """A ROS Twist (m/s, rad/s) as a NervLynx drive command (-1..1), clamped."""
  vx = float(twist.get("linear", {}).get("x", 0.0))
  wz = math.degrees(float(twist.get("angular", {}).get("z", 0.0)))
  linear = max(-1.0, min(1.0, vx / max_linear_mps)) if math.isfinite(vx) else 0.0
  angular = max(-1.0, min(1.0, wz / max_angular_dps)) if math.isfinite(wz) else 0.0
  return {"linear": round(linear, 4), "angular": round(angular, 4)}


def _parse_links(raw: Any, allowed: tuple[str, ...], direction: str) -> list[dict[str, Any]]:
  if not isinstance(raw, list):
    raise ValueError(f"{direction} must be a list of {{topic, ros_topic, type}} mappings")
  links = []
  for idx, item in enumerate(raw):
    where = f"{direction}[{idx}]"
    if not isinstance(item, dict):
      raise ValueError(f"{where} must be a mapping")
    kind = item.get("type", "json")
    if kind not in allowed:
      raise ValueError(f"{where}.type must be one of {', '.join(allowed)}")
    if not isinstance(item.get("ros_topic"), str) or not item["ros_topic"].strip():
      raise ValueError(f"{where}.ros_topic must be a ROS topic name such as /scan")
    if kind == "compressed_image":
      if not isinstance(item.get("camera"), str):
        raise ValueError(f"{where}: compressed_image needs camera: <name>")
    elif not isinstance(item.get("topic"), str) or not item["topic"].strip():
      raise ValueError(f"{where}.topic must be a NervLynx topic name")
    links.append({**item, "type": kind})
  return links


class Ros2Bridge(LiveNode):
  """Publishes NervLynx topics to ROS 2 and feeds ROS 2 topics back in."""

  rate_hz = 50.0

  def __init__(
    self,
    *,
    node_name: str = "nervlynx",
    namespace: str = "",
    to_ros: list[dict[str, Any]] | None = None,
    from_ros: list[dict[str, Any]] | None = None,
    max_linear_mps: float = 0.5,
    max_angular_dps: float = 90.0,
    base_frame: str = "base_link",
  ) -> None:
    """`max_linear_mps` and `max_angular_dps` are what full speed (1.0) on the drive
    means, so Nav2's velocities map onto the robot; faster requests are clamped."""
    if max_linear_mps <= 0 or max_angular_dps <= 0:
      raise ValueError("max_linear_mps and max_angular_dps must be > 0")
    self.node_name = node_name
    self.namespace = namespace
    self.to_ros = _parse_links(DEFAULT_TO_ROS if to_ros is None else to_ros, TO_ROS_TYPES, "to_ros")
    self.from_ros = _parse_links(DEFAULT_FROM_ROS if from_ros is None else from_ros, FROM_ROS_TYPES, "from_ros")
    self.max_linear_mps = float(max_linear_mps)
    self.max_angular_dps = float(max_angular_dps)
    self.base_frame = base_frame
    self.input_topics = tuple(dict.fromkeys(link["topic"] for link in self.to_ros if link["type"] != "compressed_image"))
    self._routes: dict[str, list[dict[str, Any]]] = {}
    for link in self.to_ros:
      if link["type"] != "compressed_image":
        self._routes.setdefault(link["topic"], []).append(link)
    self._incoming: deque[tuple[str, str, dict[str, Any]]] = deque(maxlen=256)
    self._lock = threading.Lock()
    self._rclpy: Any = None
    self._node: Any = None
    self._executor: Any = None
    self._spinner: threading.Thread | None = None
    self._owns_context = False
    self._publishers: dict[int, Any] = {}
    self._tf: Any = None
    self._msg_types: dict[str, Any] = {}
    self._camera_seq: dict[str, int] = {}
    self._camera_last: dict[str, float] = {}
    self.sent = self.received = self.errors = 0

  # ------------------------------------------------------------------ ROS plumbing

  def _types(self) -> dict[str, Any]:
    from geometry_msgs.msg import TransformStamped, Twist  # type: ignore[import-not-found]
    from nav_msgs.msg import Odometry  # type: ignore[import-not-found]
    from sensor_msgs.msg import CompressedImage, Imu, LaserScan, NavSatFix, Range  # type: ignore[import-not-found]
    from std_msgs.msg import Bool, String  # type: ignore[import-not-found]

    return {
      "odometry": Odometry,
      "laser_scan": LaserScan,
      "imu": Imu,
      "range": Range,
      "nav_sat_fix": NavSatFix,
      "compressed_image": CompressedImage,
      "bool": Bool,
      "json": String,
      "twist": Twist,
      "transform": TransformStamped,
    }

  def setup(self, ctx: NodeContext) -> None:
    try:
      import rclpy  # type: ignore[import-not-found]
      from rclpy.executors import SingleThreadedExecutor  # type: ignore[import-not-found]
      from rclpy.qos import QoSProfile, qos_profile_sensor_data  # type: ignore[import-not-found]

      self._msg_types = self._types()
    except ImportError as exc:
      raise ModuleNotFoundError(_ROS_HINT) from exc
    self._rclpy = rclpy
    if not rclpy.ok():
      rclpy.init(args=None)
      self._owns_context = True
    self._node = rclpy.create_node(self.node_name, namespace=self.namespace or None)
    reliable = QoSProfile(depth=10)
    for link in self.to_ros:
      qos = qos_profile_sensor_data if link["type"] in ("laser_scan", "imu", "range", "compressed_image") else reliable
      self._publishers[id(link)] = self._node.create_publisher(self._msg_types[link["type"]], link["ros_topic"], qos)
      if link.get("tf"):
        from tf2_ros import TransformBroadcaster  # type: ignore[import-not-found]

        self._tf = TransformBroadcaster(self._node)
    for link in self.from_ros:
      self._node.create_subscription(self._msg_types[link["type"]], link["ros_topic"], lambda msg, _link=link: self._on_ros(_link, msg), reliable)
    self._executor = SingleThreadedExecutor()
    self._executor.add_node(self._node)
    self._spinner = threading.Thread(target=self._executor.spin, name="nervlynx-ros2", daemon=True)
    self._spinner.start()

  def _stamp(self) -> Any:
    return self._node.get_clock().now().to_msg()

  def _fill(self, msg: Any, data: dict[str, Any]) -> Any:
    for key, value in data.items():
      if isinstance(value, dict):
        self._fill(getattr(msg, key), value)
      else:
        setattr(msg, key, value)
    return msg

  def _publish(self, link: dict[str, Any], data: dict[str, Any]) -> None:
    msg = self._fill(self._msg_types[link["type"]](), data)
    if "header" in data:
      msg.header.stamp = self._stamp()
    self._publishers[id(link)].publish(msg)
    self.sent += 1

  def _to_ros(self, link: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    kind = link["type"]
    frame = link.get("frame_id", _FRAME_IDS.get(kind, ""))
    if kind == "odometry":
      return odometry_to_ros(payload, frame_id=frame, child_frame_id=link.get("child_frame_id", self.base_frame))
    if kind == "laser_scan":
      return scan_to_ros(payload, frame_id=frame)
    if kind == "imu":
      return imu_to_ros(payload, frame_id=frame)
    if kind == "range":
      return range_to_ros(payload, frame_id=frame, field_of_view_deg=float(link.get("field_of_view_deg", 30.0)))
    if kind == "nav_sat_fix":
      return gps_to_ros(payload, frame_id=frame)
    if kind == "bool":
      return {"data": bool(payload.get(link.get("field", "engaged"), False))}
    return {"data": json.dumps(payload, separators=(",", ":"), default=str)}

  def _on_ros(self, link: dict[str, Any], msg: Any) -> None:
    """ROS callback (spin thread): queue for the executor thread."""
    try:
      if link["type"] == "twist":
        twist = {"linear": {"x": msg.linear.x}, "angular": {"z": msg.angular.z}}
        payload = twist_to_drive(twist, max_linear_mps=float(link.get("max_linear_mps", self.max_linear_mps)), max_angular_dps=float(link.get("max_angular_dps", self.max_angular_dps)))
        schema = "DriveCommand"
      else:
        payload = json.loads(msg.data)
        if not isinstance(payload, dict):
          raise ValueError("JSON messages must be objects")
        schema = str(link.get("schema", "Message"))
    except (TypeError, ValueError, AttributeError):
      self.errors += 1
      return
    with self._lock:
      self._incoming.append((link["topic"], schema, payload))
    self.received += 1

  # ------------------------------------------------------------------ executor thread

  def on_message(self, msg: RuntimeMessage, ctx: NodeContext) -> Iterable[Output] | None:
    if self._node is None:
      return None
    for link in self._routes.get(msg.envelope.topic, ()):
      try:
        data = self._to_ros(link, msg.payload)
        self._publish(link, data)
        if link["type"] == "odometry" and link.get("tf") and self._tf is not None:
          transform = self._fill(self._msg_types["transform"](), odometry_transform(msg.payload, frame_id=data["header"]["frame_id"], child_frame_id=data["child_frame_id"]))
          transform.header.stamp = self._stamp()
          self._tf.sendTransform(transform)
      except Exception as exc:  # noqa: BLE001 - one bad message must not stop the bridge
        self.errors += 1
        ctx.fault(f"ros2_bridge: could not publish {msg.envelope.topic} to {link['ros_topic']}: {exc}", kind="ros2")
    return None

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    out: list[Output] = []
    with self._lock:
      incoming = list(self._incoming)
      self._incoming.clear()
    latest: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for topic, schema, payload in incoming:
      if schema == "DriveCommand":
        latest[topic] = (topic, schema, payload)  # only the newest velocity matters
      else:
        out.append((topic, schema, payload))
    out.extend(latest.values())
    self._publish_cameras(ctx)
    return out or None

  def _publish_cameras(self, ctx: NodeContext) -> None:
    from robot_core.camera import frames

    now = ctx.now_ns / 1e9
    for link in self.to_ros:
      if link["type"] != "compressed_image" or self._node is None:
        continue
      name = link["camera"]
      buffer = frames(name)
      frame = buffer.latest() if buffer is not None else None
      if frame is None or frame.seq == self._camera_seq.get(name) or now - self._camera_last.get(name, -1e9) < 1.0 / float(link.get("fps", 10.0)):
        continue
      self._camera_seq[name], self._camera_last[name] = frame.seq, now
      self._publish(link, {"header": {"frame_id": link.get("frame_id", "camera")}, "format": frame.content_type.split("/")[-1], "data": frame.data})

  def teardown(self, ctx: NodeContext) -> None:
    if self._executor is not None:
      self._executor.shutdown()
    if self._spinner is not None:
      self._spinner.join(timeout=2.0)
    if self._node is not None:
      self._node.destroy_node()
      self._node = None
    if self._owns_context and self._rclpy is not None:
      self._rclpy.shutdown()

  def status(self) -> dict[str, Any]:
    return {
      "ros_node": self.node_name,
      "to_ros": {link.get("topic") or f"camera {link['camera']}": link["ros_topic"] for link in self.to_ros},
      "from_ros": {link["ros_topic"]: link["topic"] for link in self.from_ros},
      "sent": self.sent,
      "received": self.received,
      "errors": self.errors,
    }
