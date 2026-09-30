# Getting Started Paths

This guide gives a fast "first successful run" for different personas.

## Student, Hobbyist, or MVP Builder

Use this path if you want a robot moving, first in simulation and then on a Raspberry Pi.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install "git+https://github.com/vedantparnaik/nervlynx"
nervlynx new my-rover
cd my-rover
nervlynx sim                     # http://127.0.0.1:9120/
```

What you get:
- A project folder: `robot.yaml` (the robot), `nodes/avoid.py` (its behaviour), and a
  `README.md` with the wiring.
- A simulated room with obstacles, drawn live in the dashboard.
- The same project runs on a Pi: `nervlynx doctor`, then `nervlynx deploy pi@<pi>.local
  --service` from the laptop (see the main README).

Where to go next:
- `nervlynx new buddy --template follow-me`: a robot that follows a person with a camera
  and a detector; in the simulator a person walks laps around the room.
- The dashboard's **Calibrate** section fixes backwards motors, IMU mounting, and servo
  limits on the real robot, and **Talk to the robot** takes commands like "turn left and
  drive forward one metre" once you add the `skills` and `agent` nodes
  ([AGENTS.md](AGENTS.md)).
- More sensors: `pca9685_servos`, `gps_nmea`, `lidar`, `wheel_odometry`
  ([LIVE_RUNTIME.md](LIVE_RUNTIME.md)); `nervlynx scan` suggests them.
- A Pi plus an Orin NX (or a laptop) as one robot: [MESH.md](MESH.md). Mapping and
  navigation with ROS 2: [ROS2.md](ROS2.md). Several robots: [FLEET.md](FLEET.md).

## Robotics Engineer

Use this path if you want to run and inspect pipeline behavior quickly.

```bash
make demo
robot-core inspect-trace logs/robot_core_trace.jsonl
```

What you get:
- A working local runtime run.
- A replay of the same trace for deterministic validation.
- A trace file to inspect execution and metadata.

## Platform Engineer

Use this path if you want to validate automation and operational hooks.

```bash
make setup
pytest -q
robot-core serve-metrics --duration-s 5 --port 9108
```

What you get:
- Reproducible local environment setup.
- Baseline test signal before integrating CI/CD changes.
- Metrics endpoint validation for runtime observability wiring.

## Robot Builder

Use this path if you want a graph that runs continuously and drives motors.

```bash
make setup
robot-core run-live examples/live/rover_sim.yaml --duration-s 30 --allow-control
# open http://127.0.0.1:9120/ and drive with W/A/S/D
```

What you get:
- A simulated skid-steer rover running live with a dashboard, teleop, and e-stop.
- A run directory under `logs/live/` with the message trace and a latency/jitter report.
- The same graph shape you deploy on a Raspberry Pi with `examples/live/rover_bts7960.yaml` (see `docs/LIVE_RUNTIME.md`).

## Student or Hobbyist

Use this path if you want the quickest end-to-end confidence loop.

```bash
make demo
robot-core smoke-surveillance --output logs/smoke_surveillance_trace.jsonl
robot-core replay logs/smoke_surveillance_trace.jsonl
```

What you get:
- A first successful run with minimal setup.
- A second smoke scenario to explore behavior changes.
- Replay confidence before trying your own plugins/configs.

## See also

- Plugin scaffolding and reference packs: `docs/PLUGIN_AUTHORING.md`
- Labels and contributor onboarding: `docs/COMMUNITY.md`
- Full command reference: `README.md` (Common Commands)
