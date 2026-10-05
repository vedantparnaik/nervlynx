# Live runtime

`robot-core run-graph` pushes one seed message through a graph and exits. A robot needs the
graph to keep running: sensors sampled at fixed rates, control loops ticking, commands
reaching the motors, and a dependable way to stop. Live mode does that with the same
envelopes, topics, and plugins, and adds the pieces you need when hardware is attached:

- **Continuous executor** (`LiveRuntime`): fixed-rate ticks on the real clock or a
  simulated one, priority dispatch, per-step hop budget, deterministic trace IDs in sim.
- **Safety layers**: drive deadman, latched e-stop, liveness watchdog, per-node circuit
  breaker, and an out-of-band stall guard that stops actuators if the executor hangs.
- **Hardware**: gpiozero / RPi.GPIO / mock pin backends plus `auto` (real pins on a
  Raspberry Pi, mock everywhere else), BTS7960 and TB6612 motor drivers, and a
  skid-steer drive node with stiction kick, slew limiting, and duty floors.
- **Observability**: live dashboard with teleop, Prometheus `/metrics`, JSON `/stats`,
  `robot-core top` for SSH sessions, and a run directory (trace, faults, report) for
  every session.

## Quick start (no hardware)

```bash
make setup
robot-core run-live examples/live/rover_sim.yaml --duration-s 30
# open http://127.0.0.1:9120/ while it runs
```

A scripted driver commands a simulated skid-steer rover: the drive node shapes commands
onto mock BTS7960 motors and a kinematic model turns the applied duty into odometry.
When the run ends (duration reached or Ctrl-C) a Markdown report is printed and the run
directory is written to `logs/live/rover_sim-<timestamp>/`.

Other ways to run it:

```bash
# Deterministic and as fast as the CPU allows (CI, soak tests, tuning experiments)
robot-core run-live examples/live/rover_sim.yaml --sim-time --duration-s 600

# Drive it yourself from the dashboard (WASD / arrow keys)
robot-core run-live examples/live/rover_sim.yaml --allow-control

# Watch a running graph from a terminal (e.g. over SSH on the robot)
robot-core top http://127.0.0.1:9120
```

## Running on a Raspberry Pi robot

1. Install NervLynx on the Pi and make a GPIO library available to its virtualenv:

   ```bash
   sudo apt install python3-gpiozero python3-lgpio   # works on every Pi, including the Pi 5
   python3 -m venv --system-site-packages .venv      # lets the venv see the apt packages
   .venv/bin/pip install -e .                        # or: pip install -e ".[pi]"
   ```

   RPi.GPIO does not support the Pi 5 (its GPIO sits behind the RP1 chip), so prefer
   gpiozero. With `hardware.backend: auto`, NervLynx picks gpiozero on a Pi, falls back to
   RPi.GPIO only on boards where it works, and uses mock pins on any other machine, so the
   same config runs in simulation on a laptop and on the robot.

2. Copy one of the hardware packs and edit the pins to match your wiring:

   | Pack | Hardware |
   | --- | --- |
   | `examples/live/rover_bts7960.yaml` | 4x BTS7960 (IBT-2), one motor per driver, RPi.GPIO backend |
   | `examples/live/rover_tb6612.yaml` | 2x TB6612FNG (front/rear axle), gpiozero backend |

3. Validate without touching the pins, then run with the wheels off the ground:

   ```bash
   robot-core live-validate examples/live/rover_bts7960.yaml
   robot-core run-live examples/live/rover_bts7960.yaml --backend mock --duration-s 5   # dry run
   robot-core run-live examples/live/rover_bts7960.yaml --host 0.0.0.0 --allow-control
   ```

   Open `http://<pi-address>:9120/` on a laptop and hold W/A/S/D. The motors stop 0.25 s
   after you let go (deadman), and the red **E-STOP** button always works, with or without
   control access. The packs cap speed with `max_speed: 0.6`; raise it once the robot
   behaves.

4. To run at boot, see `deploy/systemd/nervlynx-rover.service`. It sends SIGINT on stop so
   motors are stopped and the report is written before the process exits, and lets
   NervLynx arm systemd's watchdog so a frozen process is restarted (see the
   [safety model](#safety-model)).

Only one process can own the motor pins. Stop any other motor server before starting a
hardware graph.

## Graph config reference

```yaml
name: my_robot                 # used for the run directory and metrics
description: free text

runtime:
  clock: system                # or simulated (same as --sim-time)
  max_queue_size: 4096         # messages beyond this are dropped and counted
  max_hops_per_step: 4096      # dispatch budget per scheduling step
  stall_timeout_s: 0.5         # stall guard window; null disables it
  max_idle_sleep_s: 0.05       # longest the executor sleeps between steps
  breaker: {threshold: 5, cooldown_s: 2.0}   # threshold 0 disables breakers
  topic_priority: {safety.estop: 0}          # lower value dispatches first (default 100)
  latest_topics: [range.front, camera.frame] # deliver only the newest pending message
  max_inbox_size: 4096         # messages waiting from other threads; extra ones are dropped
  systemd_watchdog_s: 5        # as a service: restart if the executor stops this long; null disables
  seed: 7                      # trace-ID seed (defaults to 0 on a simulated clock)

safety:
  stale_after_s: 0.5           # default liveness window for critical nodes
  estop_on_stale: true         # latch the e-stop when a critical node goes stale
  start_in_estop: false        # start latched; clear from the dashboard to move

hardware:
  backend: auto                # default for nodes that take a backend; --backend overrides

nodes:
  - name: drive                # unique; defaults to the plugin name
    plugin: skid_steer_drive   # live node, one-shot node plugin, or sensor plugin
    rate_hz: 50                # tick rate (required for sensors, optional otherwise)
    input_topics: [cmd.drive]  # optional; defaults come from the plugin
    critical: true             # watched for liveness (see Safety)
    stale_after_s: 0.3         # per-node override of safety.stale_after_s
    only: robot                # optional: run only in robot mode or only in sim mode
    params: {...}              # keyword arguments for live node plugins
  - name: camera
    plugin: camera_ingest_sensor
    rate_hz: 10
    topic: sensors.bundle      # sensors publish each reading here
    schema: SensorBundle
```

`plugin` is resolved in this order:

1. **Live node factories** (`nervlynx.live_nodes` entry points and the built-ins below),
   constructed with `params`.
2. **One-shot node plugins** (`nervlynx.nodes`): their `handle(msg)` runs on every
   message on `input_topics`, unchanged.
3. **Sensor plugins** (`nervlynx.sensors`): `read()` is sampled every tick and published
   on `topic`.

`robot-core live-validate` checks structure, plugin existence, pin conflicts, and node
parameters by constructing every node; constructors never touch hardware, so validation
is safe on any machine.

### One config for simulation and the robot

Mark nodes that only make sense in one place with `only: sim` or `only: robot`; unmarked
nodes run in both. `run-live --mode robot` (the default) skips `only: sim` nodes; `--mode
sim` skips `only: robot` nodes and forces every hardware node onto mock pins unless you
pass `--backend`. `live-validate` checks each mode separately when a config uses `only:`.

```yaml
nodes:
  - {plugin: avoid}                          # your code: runs in both
  - {plugin: skid_steer_drive, params: ...}  # mock pins in sim, real pins on the robot
  - {plugin: hcsr04_range, only: robot, params: {trigger: 23, echo: 24}}
  - {plugin: skid_steer_sim, only: sim, params: {world: ..., range_sensors: [{name: front}]}}
```

### Calibration wizard and per-robot overlays

Run with dashboard control (`nervlynx run --control`, or `nervlynx sim`) and the dashboard
shows a **Calibrate** section for every node that supports it:

- **Drive**: spin each motor forward and invert the ones that turn backward; drive forward
  and turn left, and swap sides if "left" turned right; step the power up until the robot
  just moves and keep that as `min_duty`. Tests last about a second, refuse to run while
  the e-stop is latched, and stop the moment any drive command arrives.
- **IMU**: capture the robot flat, then with its front lifted, and it works out `axes`.
- **Servos**: slide each servo towards its ends and set `min_us`, `max_us`, and home.

Changes apply immediately. **Save** writes them to `calibration.yaml` beside `robot.yaml`,
which is applied every time that robot starts. It only ever holds what you calibrated, so
`robot.yaml` stays exactly as you wrote it and can be shared between robots.

`calibration.yaml` and `overlay.yaml` (per-robot settings written by `nervlynx fleet`) are
both overlays: nodes are addressed by name, mappings merge key by key, and lists of named
items (motors, servos) merge item by item:

```yaml
nodes:
  drive:
    params:
      right: [{name: right, invert: true}]
      tuning: {min_duty: 0.22}
```

Anything an overlay names must exist in `robot.yaml`, so renaming a motor makes
`nervlynx validate` fail instead of silently dropping its calibration. `nervlynx deploy`
never copies or deletes either file on the robot, and every run records the overlays it
used next to its report.

## Built-in live nodes

| Plugin | What it is | Details |
| --- | --- | --- |
| `skid_steer_drive` | DC motors on L298N, TB6612, or BTS7960 drivers | below |
| `heartbeat` | A pin that toggles while the robot may move, so hardware can cut motor power when it stops | below |
| `hcsr04_range`, `mpu6050_imu`, `gps_nmea`, `lidar` | Distance, IMU, GPS, and LiDAR sensors | below |
| `camera`, `detector` | Pi or USB camera, and object detection on its frames | below |
| `pca9685_servos` | Hobby servos on a PCA9685 board | below |
| `wheel_odometry`, `esp32_link` | Odometry from encoders; motors and encoders on a microcontroller | below, [ESP32_LINK.md](ESP32_LINK.md) |
| `skid_steer_sim`, `scripted_drive` | The simulator (world, sensors, people, cameras) and scripted commands | below |
| `skills`, `agent`, `voice` | Named actions, plain-language and LLM agents, microphone and speaker | [AGENTS.md](AGENTS.md) |
| `ros2_bridge` | Topics to and from ROS 2 | [ROS2.md](ROS2.md) |

Every node that takes a `backend` gets `hardware.backend` by default, and `nervlynx sim`
switches them all to mock.

### `skid_steer_drive`

Consumes drive commands, applies them to motors at `rate_hz`, and publishes `drive.state`.

| Param | Default | Meaning |
| --- | --- | --- |
| `left`, `right` | required | Motor specs per side (any number of motors) |
| `driver` | `bts7960` | `bts7960` (`rpwm`, `lpwm`, optional `enable_pins`), `tb6612` (`in1`, `in2`, `pwm`, optional shared `stby`), or `l298n` (`in1`, `in2`, plus `en` for PWM on ENA/ENB; leave `en` out when the EN jumper is fitted and IN1/IN2 carry the PWM) |
| `backend` | `mock` | `mock`, `rpi_gpio`, `gpiozero`, or `auto` (or `hardware.backend` / `--backend`); `auto` reports what it chose as an info fault and in `/stats` |
| `pwm_frequency_hz` | 1000 | PWM carrier frequency |
| `deadman_s` | 0.25 | Stop when no command has arrived for this long |
| `max_speed` | 1.0 | Scales every command (a global speed limit) |
| `command_topic` / `state_topic` | `cmd.drive` / `drive.state` | Topic names |
| `state_every_n_ticks` | 2 | `drive.state` heartbeat rate (also published on each new command) |
| `tuning` | see below | Shaping for real gearmotors |
| `swap_sides` | false | The motors listed under `left` are on the robot's right (set by the calibration wizard) |

Motor spec fields: `name`, pins (BCM numbers), and `invert: true` for motors mounted
mirrored. Pin conflicts are rejected at validation time.

Command payloads (all -1..1): `{"left": l, "right": r}` or `{"linear": v, "angular": w}`.

Tuning (`tuning:`): `min_duty` 0.18 and `turn_min_duty` 0.26 remap any non-zero request
onto `[floor, 1]` so small commands still move; `kick_duty` 0.55 for `kick_s` 0.18 breaks
static friction when a side starts from rest (re-armed after `kick_rearm_s` 0.5 at rest);
`slew_per_s` 4.0 ramps duty so four motors never step to full together and sag the
battery; reversals coast through zero. Stops are always immediate.

### `heartbeat`

Every stop in software needs the software to be running. The `heartbeat` node covers the
rest: it toggles a pin on every tick, and holds it low while the e-stop is latched, from
the moment the stall guard fires, and at shutdown. When NervLynx crashes, freezes, or the
Pi loses power, the pin simply stops changing, and hardware that watches it can turn the
motors off by itself.

| Param | Default | Meaning |
| --- | --- | --- |
| `pin` | required | BCM pin. Avoid the I2C, SPI, and UART pins; GPIO 26 is a good choice |
| `frequency_hz` | 25 | Square-wave frequency; the node ticks at twice this |
| `backend` | `mock` | As for the drive (or `hardware.backend`) |

Between the pin and the motor driver, put something that keeps the driver enabled only
while rising edges keep arriving: a retriggerable monostable such as a 74HC123 or CD4538
on its rising-edge input, or a small microcontroller doing the same. Its output goes to
the driver's enable: STBY on a TB6612, R_EN and L_EN on a BTS7960, or ENA and ENB on an
L298N (through an AND gate with the PWM when `en` carries it). A timeout of a few periods,
around 100 ms, stops the robot quickly without tripping on normal jitter; the run
report's lateness column for the `heartbeat` node shows how late its ticks get. Never
wire the pin straight to an enable input: if NervLynx froze while the pin was high, the
motors would stay powered.

Test it with the wheels off the ground: while the motors turn, freeze NervLynx with
`kill -STOP <pid>`, and the wheels must stop within the timeout. The node is tested in
CI; a cut-off circuit built this way has not been tested on a robot yet.

### `scripted_drive`

Publishes a timed command sequence (`steps: [{left, right, duration_s} | {linear, angular, duration_s}]`,
`loop`, `start_delay_s`, `topic`). A non-looping script goes quiet at the end, which
exercises the deadman.

### `skid_steer_sim`

First-order kinematic plant driven by `drive.state` duty: `max_speed_mps`, `track_width_m`,
`time_constant_s`, `stiction_duty`, `odom_every_n_ticks`. Publishes `odom` with pose,
speed, yaw rate, and distance.

Give it a `world` and it becomes a small 2D simulator you can develop obstacle avoidance
against before the robot exists:

```yaml
- name: sim
  plugin: skid_steer_sim
  rate_hz: 50
  params:
    world:
      width_m: 4.0                         # walls at x = 0..4, y = 0..3
      height_m: 3.0
      obstacles:
        - {circle: [3.0, 1.5, 0.25]}       # x, y, radius
        - {box: [0.5, 2.2, 1.0, 2.8]}      # x_min, y_min, x_max, y_max
    start: [1.0, 1.0, 0.0]                 # x, y, heading_deg (default: arena centre)
    robot_radius_m: 0.12
    range_sensors:                         # publish range.<name>
      - {name: front, angle_deg: 0, max_range_m: 2.0, noise_m: 0.01}
      - {name: left, angle_deg: 45, max_range_m: 1.0}
    range_every_n_ticks: 2                 # 25 Hz at rate_hz 50
    lidar: {range_max_m: 8.0, noise_m: 0.01}   # 360-degree scans on `scan`, like the lidar node
    seed: 0                                # sensor noise is repeatable per seed
```

People and other moving things go in `world.targets`, and simulated `cameras` see them
the way the `detector` node would, publishing the same payload on `detections.<name>`:

```yaml
    world:
      targets:
        - {label: person, path: [[1, 1], [6, 1], [6, 4]], speed_mps: 0.3}   # loops; loop: false walks back
        - {label: dog, at: [2.0, 3.5], radius_m: 0.25}                      # stands still
    cameras:
      - {name: front, fov_deg: 62, range_m: 6.0}   # angle_deg, height_m, labels, every_n_ticks too
```

Targets block the robot, its range sensors, and its LiDAR, are hidden behind obstacles,
and wait rather than walk into the robot. A box's height shrinks with distance just as a
real person's does, which is what the follow-me template steers by.

`lidar` takes `topic` (`scan`), `bins` (360), `range_min_m` / `range_max_m` (0.05 / 8.0),
`noise_m`, and `every_n_ticks` (5, so 10 Hz at `rate_hz` 50); simulated scans also carry
the `pose` they were taken from. The dashboard draws the scan in the world view and in a
LiDAR panel; the panel also shows a real `lidar` node's scans, and `GET /topic/scan`
returns the newest one in full.

Range readings are `{"distance_m", "max_range_m", "hit"}`, measured from the robot's edge
and capped at `max_range_m` (`hit` is false when nothing is in range). Walls and obstacles
stop the robot: it stalls at the contact point, each new contact counts as a collision
(`nervlynx_sim_collisions_total`, a `sim_collision` fault, and `collisions` in `/stats`),
and it counts again only after it has moved 1 cm clear.

### `hcsr04_range`

HC-SR04 ultrasonic ranger, read through gpiozero's `DistanceSensor` (which times the echo
on its own thread, so the executor never blocks). Publishes the same payload as the
simulator on `range.<name>` at 15 Hz by default.

| Param | Default | Meaning |
| --- | --- | --- |
| `trigger`, `echo` | required | BCM pins. **The echo pin outputs 5 V**: use a divider (1 kΩ + 2 kΩ) into the Pi |
| `name` / `topic` | `front` / `range.<name>` | Output topic |
| `max_range_m` | 2.0 | Readings are capped here; `hit` is false at the cap |
| `backend` | `mock` | `gpiozero` or `auto` on the robot; mock publishes `mock_distance_m` (default: nothing in range) |

### `camera`

Pi camera (picamera2, hardware-friendly JPEG encoder) or USB webcam (OpenCV), streamed to
the dashboard at `/camera/<node>.mjpg` (latest frame: `/camera/<node>/latest`). Frames
never travel through the message bus: they sit in a latest-frame buffer that other nodes
read with `robot_core.camera.frames("<name>")`, and the node publishes only metadata on
`camera.<name>` (`{"seq", "width", "height", "bytes", "fps", "format"}`).

| Param | Default | Meaning |
| --- | --- | --- |
| `name` | `front` | Buffer and topic name |
| `width` / `height` / `fps` | 640 / 480 / 15 | Capture size and rate (also the node's tick rate) |
| `source` | `auto` | `auto` (picamera2 if a Pi camera is attached, else OpenCV; mock off the Pi), `picamera2`, `opencv`, or `mock` |
| `device` / `quality` | 0 / 80 | OpenCV device index and JPEG quality |
| `optional` | false | When true, a missing camera is a warning and the rest of the robot keeps running |

The mock source draws a moving colour-bar pattern (PNG, standard library only) inside the
tick, so simulated-clock runs stay deterministic.

### `mpu6050_imu`

MPU6050-family IMU (MPU6050, MPU6500, MPU9250 accel and gyro) over I2C with `smbus2`.
Publishes `{"accel_mps2": [x, y, z], "gyro_dps": [x, y, z], "temp_c"}` on `imu` at 50 Hz.
It checks the chip's WHO_AM_I register at start-up, and averages `calibrate_samples` (100)
gyro readings to remove bias, so keep the robot still while it starts. I2C failures become
a fault that says how to check the wiring (`i2cdetect -y 1`).

| Param | Default | Meaning |
| --- | --- | --- |
| `bus` / `address` | 1 / `0x68` | `0x69` when AD0 is pulled high |
| `accel_range_g` / `gyro_range_dps` | 2 / 250 | Full-scale ranges |
| `axes` | `["+x", "+y", "+z"]` | Which signed IMU axis points along the robot's forward, left, and up; readings are published in the robot's frame. The calibration wizard works it out from two poses; mirror-image mappings are rejected |
| `backend` | `mock` | Any real backend (e.g. `auto`) uses the I2C bus; mock reports 1 g on z and zero rotation |

### `pca9685_servos`

Hobby servos on a PCA9685 16-channel PWM board over I2C (`smbus2`). The board times the
pulses itself, so servos don't twitch the way software PWM makes them. Commands on
`cmd.servo` are angles by servo name, `{"pan": 45, "tilt": 100}` (or `{"name": "pan",
"deg": 45}`); state is published on `servo.state` at 10 Hz.

```yaml
- name: arm
  plugin: pca9685_servos
  params:
    address: 0x40
    on_stop: hold                # or relax: cut the pulses on e-stop and at shutdown
    servos:
      - {name: pan, channel: 0, min_us: 600, max_us: 2400, max_speed_dps: 180}
      - {name: tilt, channel: 1, min_deg: 30, max_deg: 150, home_deg: 90, invert: true}
```

| Servo field | Default | Meaning |
| --- | --- | --- |
| `name`, `channel` | required | Unique name and PCA9685 output 0..15 |
| `min_us` / `max_us` | 1000 / 2000 | Pulse widths at the ends of travel (400..2600); find them with the calibration wizard so the servo never buzzes against its end stop |
| `min_deg` / `max_deg` | 0 / 180 | Angles those pulses mean; commands are clamped to this range |
| `home_deg` | middle | Where the servo goes at start-up |
| `max_speed_dps` | 360 | Slew limit so a servo never slams across its range |
| `invert` | false | Reverse the direction |

Commands are ignored while the e-stop is latched. With `on_stop: hold` (the default) the
servos stay where they were, which keeps an arm from dropping what it holds; with
`relax` they go limp and stay limp until a command moves them again. Power servos from
their own 5-6 V supply (the board's V+ terminal), never from the Pi's 5 V pin.

### `gps_nmea`

Any NMEA 0183 GPS (u-blox NEO-6M/7M/M8N/M9N and most others) on USB or the Pi's UART
(`/dev/serial0`: GPS TX to GPIO15, enabled with `raspi-config` > Interface Options > Serial
Port, login shell off). Sentences are checksum-checked and read without blocking; GGA and
RMC become a fix on `gps`:

```json
{"fix": true, "lat_deg": 51.4779, "lon_deg": -0.0015, "alt_m": 45.2, "sats": 9, "hdop": 0.9,
 "speed_mps": 0.4, "course_deg": 87.0, "quality": "gps", "utc": "12:35:19"}
```

| Param | Default | Meaning |
| --- | --- | --- |
| `port` | `auto` | A u-blox/PL2303 USB receiver if one is plugged in, else `/dev/serial0` |
| `baud` | 9600 | Most modules ship at 9600 |
| `timeout_s` | 3.0 | Report a fault (with wiring hints) when no sentence arrives for this long |
| `mock_origin` | `[51.4779, -0.0015]` | Where the mock receiver is; in the simulator it follows `odom`, treating the sim's x as east and y as north |

Until the receiver has a fix (from 30 s to a few minutes under open sky after a cold
start) `fix` is false and the position is `None`. `robot_core.gps` also has
`distance_m`, `bearing_deg`, and `offset` for waypoint code.

### `lidar`

2D LiDAR: LDROBOT LD19/LD06 (`model: ld19` or `ld06`, 230400 baud) or any Slamtec RPLidar
(`model: rplidar`: A1/A2M8 at 115200, A2M12/A3/S1 at 256000, C1 at 460800; set `baud`).
A background thread parses the serial stream (LD packets are CRC-checked; RPLidar samples
are validated and the motor is started and stopped), and each whole revolution is
published on `scan`:

```json
{"ranges_m": [0.82, null, ...], "angle_min_deg": 0.0, "angle_increment_deg": 1.0,
 "range_min_m": 0.05, "range_max_m": 12.0, "nearest": {"distance_m": 0.42, "angle_deg": 348.0},
 "points": 452, "scan_hz": 10.0, "model": "ld19"}
```

`ranges_m[i]` is the closest return in the bin at `i * angle_increment_deg`,
counter-clockwise from the robot's front (90 is left, as in ROS), or `null` when nothing
came back. `robot_core.lidar.sector_min(scan, center_deg, width_deg)` gives the nearest
thing in a sector, e.g. `sector_min(scan, 0, 60)` straight ahead.

| Param | Default | Meaning |
| --- | --- | --- |
| `model` | required | `ld19`, `ld06`, or `rplidar` |
| `port` / `baud` | `auto` / per model | `auto` picks the one CP210x USB adapter |
| `bins` | 360 | Angular bins per scan |
| `range_min_m` / `range_max_m` | 0.05 / 12.0 | Returns outside this are dropped |
| `mount_deg` / `upside_down` | 0 / false | How the LiDAR's zero is turned relative to the robot's front (counter-clockwise), and whether it is mounted upside down |
| `motor_pwm` | 660 | RPLidar A2/A3 motor speed (A1 and C1 run from DTR) |
| `mock_range_m` | none | What the mock sees in every direction (default: nothing) |

The simulator publishes the same scans from its world with `skid_steer_sim`'s `lidar:`
setting, and the dashboard draws them.

### `wheel_odometry`

Pose and speed from wheel encoders (a differential drive), published on `odom` in the
simulator's format (`x_m`, `y_m`, `heading_deg`, `speed_mps`, `yaw_rate_dps`,
`distance_m`), so the ROS 2 bridge, the `drive` and `turn` skills, and your own nodes work
the same on the robot. It reads cumulative counts (`{"left_ticks", "right_ticks"}`, as
`esp32_link` publishes on `link.encoders`).

```yaml
- plugin: wheel_odometry
  params: {ticks_per_meter: 4700, track_width_m: 0.16}   # or wheel_diameter_m + ticks_per_rev
```

Measure `ticks_per_meter` by driving a metre in a straight line, and `track_width_m`
between the wheels' contact points (tune it until a 360-degree turn reads 360).
`invert_left` / `invert_right` flip an encoder that counts backwards. Wheel odometry
drifts, heading most of all.

### `detector`

Object detection on a camera's frames. A worker thread takes each new frame from
`frames(camera)` (a camera on this computer, or one shared from another device by the
mesh), runs the model, and the node publishes on `detections.<camera>`:

```json
{"seq": 812, "width": 640, "height": 480, "latency_ms": 23.1, "engine": "onnx", "model": "yolox-nano",
 "detections": [{"label": "person", "confidence": 0.87, "box": [0.43, 0.13, 0.77, 0.91],
                 "center": [0.6, 0.52], "size": [0.34, 0.78]}]}
```

Boxes are fractions of the image (x right, y down), so code works at any resolution. The
model never runs on the executor thread: a slow model lowers the detection rate, not the
control loop.

| Param | Default | Meaning |
| --- | --- | --- |
| `camera` | `front` | Which camera's frames to read |
| `engine` | `auto` | `onnx` (ONNX Runtime on the CPU), `tensorrt` (ONNX Runtime's TensorRT provider on a Jetson; engines are cached after the first start), `opencv` (OpenCV DNN), `hailo` (Raspberry Pi AI Kit / AI HAT+ with a `.hef` model), `auto`, or `mock` |
| `model` | `yolox-nano` | `yolox-nano` or `yolox-tiny` (Apache-2.0, downloaded once and checksum-checked), a `.onnx` path (YOLOX or YOLOv8/YOLO11 exports), or a `.hef` path or name from `/usr/share/hailo-models` |
| `labels` | all | Only report these classes, e.g. `[person]` |
| `min_confidence` / `iou_threshold` | 0.5 / 0.45 | Detection threshold and overlap suppression |
| `max_fps` | 10 | Upper bound on inference rate |
| `download` | true | Download a named model on first use; with false, run `nervlynx models get <name>` beforehand |
| `class_names` | COCO's 80 | Class names for a custom model |

Install with `pip install "nervlynx[ai]"` (numpy, ONNX Runtime, Pillow; OpenCV is used for
decoding when present). `nervlynx models` lists the named models and whether they are
downloaded. On a Pi 5 CPU, yolox-nano runs at several frames a second; on an Orin NX use
`engine: tensorrt`, and to run it on the Orin while the camera is on the Pi, place the
detector there and share the camera with `mesh.frames` (see [MESH.md](MESH.md)). The
simulator publishes the same payload from its world (`skid_steer_sim` `cameras:`). The
Hailo and TensorRT backends have not been run on that hardware yet.

## Safety model

Layered so that no single failure leaves motors running:

| Layer | Trigger | Effect |
| --- | --- | --- |
| Deadman (drive node) | No fresh command within `deadman_s` | Motors to zero until a new command arrives |
| E-stop latch | `POST /estop`, a node's `ctx.engage_estop()`, watchdog, breaker on a critical node, stall guard | Every node's `safe_stop()`; stays latched until cleared |
| Watchdog | A `critical` node has not ticked or handled a message within its stale window | Fault plus e-stop (with `estop_on_stale`) |
| Circuit breaker | A node raises `threshold` times in a row | Node skipped for `cooldown_s`, then retried; its `safe_stop()` runs |
| Stall guard (separate thread) | The executor has not completed a step within `stall_timeout_s` | Every node's thread-safe `hard_stop()` from outside the executor, then e-stop |
| Shutdown / Ctrl-C / SIGTERM | Process exit | `safe_stop()` and `hard_stop()` on all nodes before teardown |
| systemd watchdog (separate process) | Running as a service, and the executor has completed no step for about `systemd_watchdog_s` | systemd kills NervLynx, with every thread's stack in the journal, and starts it again |
| Heartbeat pin (`heartbeat` node plus a small circuit) | The pin stops toggling: e-stop, stall guard, shutdown, a crash, a freeze, or lost power | The circuit disables the motor driver, with no software involved |

E-stop details: engaging it from HTTP calls `hard_stop()` immediately on the request
thread, then latches on the next step. Clearing is refused while any critical node is
stale. After a clear the drive stays stopped until it receives a fresh command, so an old
command cannot resume motion.

The stall guard stops actuators before it records anything, and run logs are written on
their own threads, so a slow SD card never delays a stop.

The systemd watchdog is armed only once the executor is stepping, so slow start-up never
trips it, and only by units that allow it (`NotifyAccess=main`, as in the units
`nervlynx deploy --service` writes and `deploy/systemd/`). It restarts a frozen process but
cannot stop that process's motors meanwhile; that is what the heartbeat pin, or a motor
board with its own command timeout such as the [ESP32 link](ESP32_LINK.md), is for. A
restarted robot starts as if it had just booted; set `safety.start_in_estop: true` if it
should wait for an operator to clear the e-stop. CI checks the watchdog against real
systemd (`deploy/systemd/check_watchdog.sh`): a slow start and a healthy run are left
alone, and a frozen process or a hung control loop is restarted.

## Observability

### HTTP endpoints (default `127.0.0.1:9120`)

| Endpoint | Purpose |
| --- | --- |
| `GET /` | Live dashboard: health, nodes, topics with last payloads, faults, e-stop, teleop pad |
| `GET /metrics` | Prometheus text format |
| `GET /health` | `ok`, `degraded` (breaker open), `fault` (stale critical node), or `estop` |
| `GET /stats` | Full JSON snapshot (what the dashboard and `robot-core top` read) |
| `GET /graph` | Nodes, topics, subscriptions |
| `GET /faults` | Recent structured faults |
| `POST /estop` | Latch the e-stop (always allowed) |
| `POST /estop/clear` | Needs `--allow-control` (and the token, if set) |
| `POST /publish` | `{"topic","schema","payload"}`; needs control access and a `--control-topic` |
| `GET /calibration` | Calibratable nodes, what the wizard shows for each, and the saved `calibration.yaml` |
| `POST /calibration/<node>` | `{"action", ...}`: one wizard step, run on the executor thread; needs control access |
| `POST /calibration/save` | Write `calibration.yaml`; needs control access |

Use `--host 0.0.0.0` to reach the dashboard from another machine, and set
`--control-token` (or `NERVLYNX_CONTROL_TOKEN`) on shared networks; the dashboard passes
`?token=` through to its requests.

### Metrics

| Metric | Labels | Meaning |
| --- | --- | --- |
| `nervlynx_messages_published_total` | topic, source | Messages published |
| `nervlynx_messages_delivered_total` | topic, node | Deliveries to nodes |
| `nervlynx_messages_dropped_total` | topic, reason | `backpressure` or `breaker_open` |
| `nervlynx_handler_seconds` | node | Histogram of `on_message` time |
| `nervlynx_tick_seconds` | node | Histogram of `tick` time |
| `nervlynx_tick_lateness_seconds` | node | Histogram of how late ticks start (jitter) |
| `nervlynx_tick_overruns_total` | node | Ticks a full period or more late |
| `nervlynx_trace_latency_seconds` | topic | Root message to this topic, e.g. command to actuation on `drive.state` |
| `nervlynx_node_errors_total`, `nervlynx_node_breaker_trips_total`, `nervlynx_node_breaker_open` | node | Failures and breaker state |
| `nervlynx_watchdog_faults_total` | node | Critical nodes that went stale |
| `nervlynx_estop_engaged`, `nervlynx_estop_events_total` | source | E-stop state and engagements |
| `nervlynx_executor_stalls_total` | | Stall guard activations |
| `nervlynx_faults_total` | kind, severity | Every recorded fault |
| `nervlynx_queue_depth`, `nervlynx_topic_rate_hz`, `nervlynx_node_heartbeat_age_seconds`, `nervlynx_uptime_seconds` | | Runtime gauges |
| `nervlynx_drive_target`, `nervlynx_drive_applied_duty` | side | Drive shaping per side |
| `nervlynx_motor_duty` | motor | Signed H-bridge duty per motor |
| `nervlynx_drive_deadman_stops_total`, `nervlynx_drive_kicks_total`, `nervlynx_drive_commands_total`, `nervlynx_drive_command_age_seconds` | | Drive behaviour |
| `nervlynx_sim_speed_mps`, `nervlynx_sim_yaw_rate_dps`, `nervlynx_sim_heading_deg`, `nervlynx_sim_distance_m` | | Simulated motion |

### Run directory

Every `run-live` session writes `logs/live/<graph>-<timestamp>/` (or `--run-dir`):

| File | Contents |
| --- | --- |
| `config.yaml` | Exact config used |
| `trace.jsonl` | Every published message; works with `robot-core replay` and `inspect-trace` (`--no-record`, `--record-exclude TOPIC`) |
| `faults.jsonl` | Structured faults as they happened |
| `report.json` | Duration, platform, message totals, per-node tick/lateness/handler percentiles, per-topic rates and trace latency, safety events, fault counts |
| `report.md` | The same report as Markdown tables (also printed at exit) |
| `metrics.prom` | Final Prometheus snapshot |

The trace and fault log are written on their own threads. In real time, when the disk
cannot keep up, new lines are dropped and counted (`trace_dropped` and
`fault_log_dropped` in `report.json`, and a line at exit) rather than slowing the robot;
simulated-time runs wait for the disk instead, so their traces are always complete.

`--strict` makes the command exit 2 if any node error, watchdog fault, stall, or e-stop
occurred, which is handy in CI and soak tests.

## Writing a node as a function

Put a file in a `nodes/` folder next to your config. Every `@node` in `nodes/*.py` becomes a
plugin the config can use by name, with no packaging or entry points:

```python
# nodes/avoid.py
from nervlynx import node


@node(inputs=["range.front"], outputs="cmd.drive", rate_hz=20)
def avoid(front, *, stop_m=0.3):
  if front["distance_m"] < stop_m:
    return {"linear": 0.0, "angular": 0.8}
  return {"linear": 0.25, "angular": 0.0}
```

```yaml
# robot.yaml
nodes:
  - plugin: avoid
    params: {stop_m: 0.4}      # overrides the keyword-only settings
```

- Positional parameters get the latest payload of each input, in order. Keyword-only
  parameters are settings; `ctx` (the `NodeContext`) and `state` (a dict kept between
  calls) are filled in when you declare them.
- With `rate_hz` the function runs at that rate on the newest inputs; without it, it runs
  on every incoming message. It waits until every input has arrived.
- It pauses while any input is older than `max_age_s` (default 1.0 s; `None` disables the
  check), so a dead sensor lets the drive deadman stop the robot instead of steering on
  stale data. `/stats` shows `waiting_for` and `stale_inputs`.
- Return `None` (publish nothing), a dict for the single output, a number/bool/string
  (published as `{"value": x}`), a dict keyed by topic for several outputs, or a list of
  `(topic, payload)` pairs. Outputs continue the trace of the newest input, so
  sensor-to-command latency shows up in the report.
- The decorated function stays a plain function, so you can unit-test it directly.
- `@node` also works on a `LiveNode` subclass (below) to set its inputs and rate.

## Writing a live node

```python
from robot_core.live import LiveNode


class BatteryMonitor(LiveNode):
  """Publishes battery voltage and latches the e-stop below a threshold."""

  def __init__(self, *, adc_channel: int = 0, min_voltage: float = 10.5) -> None:
    self.adc_channel = adc_channel   # constructors store config only; no hardware here
    self.min_voltage = min_voltage

  def setup(self, ctx):
    self.adc = open_adc(self.adc_channel)
    self.gauge = ctx.metrics.gauge("battery_voltage_v")

  def tick(self, ctx):
    volts = self.adc.read_volts()
    self.gauge.set(volts)
    if volts < self.min_voltage:
      ctx.engage_estop(f"battery at {volts:.2f} V")
    return [("power.battery", "BatteryState", {"voltage_v": volts})]

  def teardown(self, ctx):
    self.adc.close()
```

Hooks: `setup`, `on_message(msg, ctx)`, `tick(ctx)`, `safe_stop(ctx)`, `hard_stop()`
(thread-safe emergency stop for actuators), `teardown`, and `status()` for the dashboard.
Return `(topic, schema, payload)` tuples or call `ctx.publish(...)`; outputs of
`on_message` continue the input's trace, outputs of `tick` start new traces. Register the
class in your package:

```toml
[project.entry-points."nervlynx.live_nodes"]
battery_monitor = "my_pack.nodes:BatteryMonitor"
```

## Scheduling and timing

Each step drains the control inbox (HTTP publishes, e-stop requests), dispatches pending
messages, then runs every due tick in registration order and dispatches that tick's
outputs immediately, so a producer registered before its consumer reaches it in the same
step. A per-step hop budget keeps a message loop from starving the ticks; on a simulated
clock time still advances, so such a loop cannot hang a run.

On the system clock the executor sleeps until the next tick. Operating systems overshoot
sleeps (several milliseconds on macOS, around 0.1 ms on Linux), so `SystemClock` learns
the overshoot, wakes that much early, and yields through the remainder. Measure your
hardware with:

```bash
python benchmarks/benchmark_live.py --output-json logs/benchmark_live.json
```

It reports per-message dispatch cost with full instrumentation and tick lateness
percentiles and CPU use at 50/100/200 Hz.
