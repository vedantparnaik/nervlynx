# NervLynx v0.2 Roadmap

## Goals

- Mature distributed runtime mode and multi-node deployment ergonomics.
- Strengthen schema governance across Python and C++ implementations.
- Improve observability, resilience, and security posture for real deployments.

## Quarterly focus (2026)

| Quarter | Focus |
| --- | --- |
| **Q2** | Release automation, changelog discipline, contributor onboarding (`docs/COMMUNITY.md`). |
| **Q3** | Transport hardening, persistent queues, cross-process tests. |
| **Q4** | IDL-first contracts, compatibility reports in CI, richer validation. |

## Milestones

### M1 - Runtime and Transport Hardening
- [ ] Add persistent queue mode for transient transport outages.
- [ ] Add retry/backoff strategy per topic class.
- [ ] Add transport interoperability tests across process boundaries.

### M2 - Contracts and Tooling
- [ ] Add IDL-first contract generation workflow.
- [ ] Add contract compatibility report in CI artifacts.
- [ ] Add richer type validation (nested arrays/maps).

### M3 - Operations and Reliability
- [x] Add circuit breaker strategy for unstable nodes (`LiveRuntime`, `runtime.breaker`).
- [ ] Add checkpoint snapshots with version stamps.
- [ ] Add chaos scenarios for node crash/restart loops.

### M4 - Developer Experience
- [x] Publish getting-started tutorial with one robot pack (`docs/GETTING_STARTED.md`, `make demo`).
- [x] Add benchmark trend reporting in CI (baseline + artifacts).
- [x] Add package release workflow and changelog automation (`docs/RELEASE_PROCESS.md`, `.github/workflows/release.yml`).

### M5 - Live Runtime and Hardware
- [x] Continuous executor with fixed-rate ticks, simulated clock, and per-session run reports (`robot-core run-live`).
- [x] Layered actuator safety: deadman, latched e-stop, watchdog, circuit breaker, stall guard.
- [x] GPIO backends and BTS7960/TB6612 skid-steer drive with a simulated plant.
- [x] Live dashboard, teleop, Prometheus metrics, and `robot-core top`.
- [ ] Wheel encoder / IMU sensor nodes and closed-loop speed control.
- [ ] Camera source node (V4L2/MJPEG) with frame-rate and latency metrics.
- [ ] Bridge live graphs across processes over `ZmqJsonTransport`.
- [ ] Live executor parity in the C++ runtime.

### M6 - Edge-first, beginner-friendly
Phase 1 (Raspberry Pi 4/5) and most of phase 2 are in:
- [x] Core install is PyYAML + Typer; ZMQ and Cap'n Proto are extras (nothing to compile on a Zero 2 W).
- [x] `hardware.backend: auto` (gpiozero/lgpio on any Pi including the Pi 5, mock elsewhere).
- [x] `nervlynx doctor`, `scan`, `new` (obstacle-avoider, teleop), `validate`, `sim`, `run`, `deploy`, `logs`, `pull`.
- [x] `@node` functions in `nodes/*.py`; stale inputs pause the node; `only: sim|robot` for one config in both places.
- [x] 2D sim world with ultrasonic-style range sensors and collisions; dashboard world view, touch joystick, camera streams.
- [x] L298N, HC-SR04, MPU6050, and camera (picamera2/OpenCV) nodes with mock twins; latest-value topics and a bounded inbox.

Next, in board order:
- [x] Lazy `robot_core` exports: `import nervlynx` loads 5 modules instead of 31 (about 5x faster start-up).
- [ ] Zero 2 W as a first-class target: memory budget, low-res camera profile, self-hosted Pi CI runners.
- [ ] Calibration wizard in the dashboard (motor direction, IMU orientation, servo limits); PCA9685 servos; GPS; RPLidar/LD19 driver with a scan view.
- [x] ESP32 link host side: `esp32_link` node, Link v1 JSON-lines protocol, simulated board (`docs/ESP32_LINK.md`).
- [ ] ESP32 firmware tested on hardware (sketch in `firmware/esp32_link/`), browser flashing, wheel PID on the board, Pico build.
- [ ] Device mesh over Zenoh (laptop, Pi, ESP32 via zenoh-pico, Orin) with `placement:` for nodes.
- [ ] Orin NX: detector node (CPU, Hailo, TensorRT backends), follow-me template, ROS 2 bridge for SLAM/Nav2.
- [ ] Voice/LLM agents that call named skills, gated by the same safety layers.
- [ ] Teams: fleet deploys with rollback, per-robot overlays, a flashable SD image, remote access.

## Good first issues

Issues tagged **`good first issue`** and **`help wanted`** are curated for newcomers. Ideas if none are open:

1. Add one new built-in node plugin and tests.
2. Add a new chaos scenario to `robot_core/chaos.py`.
3. Add one benchmark case to `benchmarks/benchmark_runtime.py`.
4. Improve docs with a transport comparison table.
