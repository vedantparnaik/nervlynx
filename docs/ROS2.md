# ROS 2 bridge

NervLynx is the lightweight layer on the robot: drivers, the control loop, safety, and
the dashboard, small enough for a Pi Zero 2 W. ROS 2 has what is hard to rebuild:
slam_toolbox, Nav2, RViz, rosbag. The `ros2_bridge` node connects the two, so a robot
can map and navigate with ROS 2 running on an Orin NX or a laptop while NervLynx drives
the motors.

## Set up

The bridge runs inside a NervLynx process on a computer that has ROS 2 (Humble or newer).
`rclpy` comes with ROS 2, not from pip, so create the virtualenv with access to it:

```bash
source /opt/ros/humble/setup.bash
python3 -m venv --system-site-packages ~/.venv
~/.venv/bin/pip install "git+https://github.com/vedantparnaik/nervlynx"
```

Then add the node. With no settings it publishes the common topics and listens to Nav2:

```yaml
- name: ros
  plugin: ros2_bridge
  placement: orin                    # with the mesh; leave out on a single computer
  params:
    max_linear_mps: 0.5              # what full speed on the drive is, in m/s
    max_angular_dps: 90              # and full turn, in degrees per second
```

| NervLynx | ROS 2 | Type |
| --- | --- | --- |
| `odom` | `/odom` + the `odom -> base_link` transform | `nav_msgs/Odometry` |
| `scan` | `/scan` | `sensor_msgs/LaserScan` (no return is `+inf`, per REP-117) |
| `imu` | `/imu/data_raw` | `sensor_msgs/Imu` (no orientation estimate) |
| `gps` | `/fix` | `sensor_msgs/NavSatFix` (covariance from HDOP) |
| `safety.estop` | `/nervlynx/estop` | `std_msgs/Bool` |
| `cmd.drive` | `/cmd_vel` (from ROS) | `geometry_msgs/Twist`, scaled by `max_linear_mps` / `max_angular_dps` and clamped |

Choose your own with `to_ros:` and `from_ros:` (each entry is `topic`, `ros_topic`, `type`,
and optionally `frame_id`):

```yaml
params:
  to_ros:
    - {topic: odom, ros_topic: /odom, type: odometry, tf: true}
    - {topic: scan, ros_topic: /scan, type: laser_scan, frame_id: laser}
    - {topic: range.front, ros_topic: /range/front, type: range, field_of_view_deg: 30}
    - {camera: front, ros_topic: /camera/image/compressed, type: compressed_image, fps: 10}
    - {topic: detections.front, ros_topic: /nervlynx/detections, type: json}
  from_ros:
    - {ros_topic: /cmd_vel, topic: cmd.drive, type: twist}
    - {ros_topic: /nervlynx/command, topic: agent.command, type: json}
```

Types to ROS: `odometry`, `laser_scan`, `imu`, `range`, `nav_sat_fix`,
`compressed_image`, `bool`, and `json` (`std_msgs/String` holding the payload). From
ROS: `twist` and `json`.

## Mapping and navigation with an Orin NX

Put the drivers on the Pi and ROS 2 on the Orin, and connect them with the mesh
([MESH.md](MESH.md)):

```yaml
devices:
  rover: {host: pi@rover.local}
  orin: {host: nvidia@orin.local}
mesh: {transport: zenoh}
nodes:
  - {name: drive, plugin: skid_steer_drive, params: ...}          # Pi
  - {name: lidar, plugin: lidar, params: {model: ld19}}           # Pi
  - {name: ros, plugin: ros2_bridge, placement: orin}             # Orin
```

On the Orin, in a shell with ROS 2 sourced:

```bash
nervlynx run --device orin
ros2 run tf2_ros static_transform_publisher --frame-id base_link --child-frame-id laser   # where the LiDAR sits
ros2 launch slam_toolbox online_async_launch.py                                          # build a map
ros2 launch nav2_bringup navigation_launch.py                                            # then navigate it
```

Set Nav2's velocity limits (`max_vel_x`, `max_vel_theta`) at or below the bridge's
`max_linear_mps` and `max_angular_dps`; faster requests are clamped. Wheel odometry is
needed for good maps: until your robot has encoders, the simulator's `odom` is the only
source.

## Safety

Velocities from ROS become `cmd.drive` messages like any other, so every NervLynx safety
layer applies: the drive deadman stops the robot when Nav2 stops publishing (or the
mesh link drops), `max_speed` still limits the motors, and the e-stop always wins. The
e-stop state is published on `/nervlynx/estop` for ROS tools to show.

## Status

The message conversions are unit-tested, and CI runs a round trip against a real ROS 2
Humble install (odometry and scans out, `/cmd_vel` in). slam_toolbox and Nav2 have not
yet been run through the bridge on a physical robot.
