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
   motors are stopped and the report is written before the process exits.

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

## Built-in live nodes

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

Motor spec fields: `name`, pins (BCM numbers), and `invert: true` for motors mounted
mirrored. Pin conflicts are rejected at validation time.

Command payloads (all -1..1): `{"left": l, "right": r}` or `{"linear": v, "angular": w}`.

Tuning (`tuning:`): `min_duty` 0.18 and `turn_min_duty` 0.26 remap any non-zero request
onto `[floor, 1]` so small commands still move; `kick_duty` 0.55 for `kick_s` 0.18 breaks
static friction when a side starts from rest (re-armed after `kick_rearm_s` 0.5 at rest);
`slew_per_s` 4.0 ramps duty so four motors never step to full together and sag the
battery; reversals coast through zero. Stops are always immediate.

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
    seed: 0                                # sensor noise is repeatable per seed
```

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
| `backend` | `mock` | Any real backend (e.g. `auto`) uses the I2C bus; mock reports 1 g on z and zero rotation |

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

E-stop details: engaging it from HTTP calls `hard_stop()` immediately on the request
thread, then latches on the next step. Clearing is refused while any critical node is
stale. After a clear the drive stays stopped until it receives a fresh command, so an old
command cannot resume motion.

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
