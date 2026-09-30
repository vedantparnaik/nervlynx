# Changelog

All notable changes to NervLynx are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Beginner CLI**: `nervlynx new <name> [--template obstacle-avoider|teleop]` creates a project (robot.yaml, nodes/, README with wiring) that runs in simulation and on a Pi; `nervlynx validate` checks both modes; `nervlynx sim` runs it with mock pins, sim-only nodes, and dashboard driving (`--fast` for a repeatable simulated clock, `--strict` also fails on collisions); `nervlynx run` runs it on the robot with the dashboard reachable from a phone and driving behind `--control`. Templates ship as package data, and the obstacle avoider is tested to drive a simulated minute without collisions.
- Simulated range sensors take `beam_deg` to model an ultrasonic cone (nearest echo across it), so single-sensor avoidance behaves like it does on a real HC-SR04.
- **`@node` decorator** (`from nervlynx import node`): write a node as a plain function. Positional parameters receive the latest payload of each input, keyword-only parameters are settings a config can override, and `ctx`/`state` are injected on request. Nodes tick at `rate_hz` or react to each message, wait for all inputs, pause on inputs older than `max_age_s` (default 1 s) so the drive deadman can stop the robot, and continue the newest input's trace. Also decorates `LiveNode` subclasses. New top-level `nervlynx` package exposes `node`, `LiveNode`, `NodeContext`, `__version__`.
- **Project nodes**: every `@node` in `nodes/*.py` next to a config is registered automatically (`robot_core/project.py`); load errors, duplicate names, and clashes with built-ins are reported as config problems by `live-validate`, `run-live`, and `doctor`. Validation now flags nodes with no inputs and no rate, which would never run. `NodeContext.rate_hz` exposes a node's schedule, and `LiveRuntime.add_node` defaults to a node's own `rate_hz`.
- **ESP32 link** (`plugin: esp32_link`, `robot_core/link.py`, `docs/ESP32_LINK.md`): NervLynx Link v1, newline-delimited JSON over USB serial. The node forwards `cmd.drive` every tick (zeros when stale or e-stopped, so the board's own watchdog stays fed), publishes `link.encoders`, reports board watchdog trips, errors, protocol mismatches, and a silent link, and finds the board on USB automatically. Reads are non-blocking. `SimulatedLinkBoard` runs it without hardware; the Arduino sketch in `firmware/esp32_link/` (L298N + encoders + motor watchdog) is not yet tested on hardware.
- **Laptop-to-robot workflow** (`robot_core/remote.py`): `nervlynx deploy pi@my-rover.local` rsyncs the project to `~/nervlynx-projects/<name>` (skipping logs, caches, and virtualenvs), runs `nervlynx validate` on the robot, and with `--service` installs and starts a systemd user service (`nervlynx-<name>`, SIGINT stop so the run report is written; `--run-args "--control"` for teleop); `--restart` restarts it after a copy. `nervlynx logs` follows the service journal and `nervlynx pull` copies recorded runs back for inspection. Remote paths are shell-quoted and hosts starting with `-` are refused.
- **`nervlynx scan`** (`robot_core/scan.py`): probes I2C like `i2cdetect` and identifies chips by ID register (MPU6050/6500/9250, ICM-20948, BNO055, VL53L0X, BME280/BMP280/BME680, ADXL345, PCA9685, SSD1306, ADCs, I/O expanders), lists USB serial devices by vendor:product ID (CP210x, CH340/CH9102, FTDI, PL2303, native-USB ESP32, Pi Pico, u-blox GPS) without writing to them, finds Pi and USB cameras, and prints (or `--output`s) YAML node entries for what NervLynx can drive. `--json` for scripts.
- **Camera node** (`plugin: camera`, `robot_core/camera.py`): Pi camera via picamera2's JPEG encoder or USB webcam via OpenCV, captured on its own thread into a latest-frame buffer (`frames(name)`), with only small metadata published on `camera.<name>`; a stdlib-only mock test pattern keeps simulations deterministic; `optional: true` keeps the robot running without a camera. The teleop template streams it.
- **Dashboard for phones and simulation**: `/camera/<node>.mjpg` streams and `/camera/<node>/latest` frames, a camera panel, a proportional touch joystick beside the key pad, a top-down view of the sim world (obstacles, trail, sensor cones, collisions), and a status column that shows `waiting for …` / stale inputs per node. Camera streams end cleanly on shutdown (`LiveHTTPServer.stopping`).
- **Beginner-kit hardware**: `l298n` motor driver (EN as PWM, or EN jumpered with PWM on IN1/IN2) for the drive node; `hcsr04_range` ultrasonic node on gpiozero's threaded `DistanceSensor`; `mpu6050_imu` I2C node (MPU6050/6500/9250) with WHO_AM_I check, range configuration, gyro bias calibration, and wiring hints on I2C errors. Sensor payloads match the simulator's and each has a mock twin (`robot_core/sensors.py`).
- **Sim and robot modes in one config**: nodes marked `only: sim` or `only: robot` run in just that mode. `run-live --mode robot|sim` selects them (sim also forces mock pins), `live-validate` checks each mode, and validation messages keep the file's node indices. The run-live body moved into `robot_core/session.py` (`run_session`, `SessionOptions`) so every CLI shares it; reports record the mode.
- **2D sim world** for `skid_steer_sim`: an arena with walls plus circle and box obstacles (`world`, `start`, `robot_radius_m`), ray-cast `range_sensors` publishing `range.<name>` with seeded noise, and collisions that stall the robot at the contact point and count each new contact (`nervlynx_sim_collisions_total`, `sim_collision` faults, `/stats`). Without `world` the plant behaves exactly as before. `robot_core.sim.SimWorld` is usable on its own.
- **Latest-value topics** (`runtime.latest_topics`): sensor readings and frames on these topics deliver only the newest pending message, both from other threads and within the dispatch queue, so a slow consumer never works through a backlog of stale data. Superseded messages are counted (`conflated` in `/stats`, `nervlynx_messages_conflated_total`). The cross-thread inbox is now bounded (`runtime.max_inbox_size`, default 4096): `publish_external` returns False and the drop is recorded as a backpressure fault, while e-stop requests are never dropped.
- **`nervlynx doctor [robot.yaml]`** (`robot_core/doctor.py`): plain-English checks with a fix for each problem: Python and virtualenv visibility of apt packages, board model, which GPIO backend `auto` picks and whether the pins are accessible, I2C/SPI state, Pi and USB cameras with their Python libraries, USB serial devices and the `dialout` group, under-voltage and throttling from `vcgencmd get_throttled`, CPU temperature, free RAM and disk, optional packages, the mDNS dashboard URL, and (with a path) config validation. `--json` for scripts; exit code 1 when something must be fixed. Pi-only checks are skipped on laptops.
- **Live runtime** (`robot_core/live.py`): `LiveRuntime` runs graphs continuously with fixed-rate node ticks on the system clock or a simulated clock (fast-forward, deterministic trace IDs), priority dispatch with a per-step hop budget, per-node exception isolation and circuit breakers, liveness watchdog for `critical` nodes, a latched e-stop, and a stall-guard thread that hard-stops actuators when the executor stops stepping. `LiveNode` hooks: `setup`, `on_message`, `tick`, `safe_stop`, `hard_stop`, `teardown`, `status`.
- Live instrumentation: per-topic publish/deliver/drop counters, handler and tick duration histograms, tick lateness (jitter) and overruns, root-to-topic trace latency (e.g. command-to-actuation), watchdog/e-stop/stall/fault counters, and topic rate gauges.
- **Hardware layer** (`robot_core/hardware.py`): `mock`, `rpi_gpio`, and `gpiozero` pin backends; `BTS7960Motor` and `TB6612Motor` drivers; pin-conflict validation before any hardware is touched. Optional `pi` extra installs RPi.GPIO and gpiozero.
- **Skid-steer drive node** (`robot_core/drive.py`): deadman, e-stop handling, duty floors, stiction kick, slew limiting, and coast-through-zero reversals; accepts `{left,right}` or `{linear,angular}` commands; publishes `drive.state` and per-motor duty metrics.
- **Simulation nodes** (`robot_core/sim.py`): `ScriptedDriveSource` and a first-order `SkidSteerSim` plant producing odometry, so the full command-to-motion loop runs on a laptop or in CI.
- Live graph YAML (`robot_core/live_config.py`) with validation that instantiates nodes without touching hardware; one-shot node plugins and sensor plugins run live unchanged. New `nervlynx.live_nodes` entry-point group.
- **Live HTTP surface** (`robot_core/server.py`): dashboard with teleop pad and e-stop, `/metrics`, `/health`, `/stats`, `/graph`, `/faults`, `POST /estop`, and `POST /estop/clear` / `POST /publish` gated by `--allow-control`, an optional token, and a topic allowlist.
- CLI: `robot-core run-live` (dashboard, signal-safe shutdown, run directory with `trace.jsonl`, `faults.jsonl`, `report.json`, `report.md`, `metrics.prom`; `--sim-time`, `--backend`, `--strict`, `--record-exclude`), `robot-core live-validate`, and `robot-core top` (terminal dashboard).
- Live packs in `examples/live/`: `rover_sim`, `rover_bts7960`, `rover_tb6612`, `surveillance_live`; `deploy/systemd/nervlynx-rover.service`.
- `benchmarks/benchmark_live.py`: live dispatch cost and tick-lateness/CPU characterisation; CI runs it plus `live-validate` and a strict 30 s rover simulation and uploads the report.
- `robot-core chaos-pass --trials N --seed S` measures drop/mutate rates over many deterministic trials.
- `robot-core inspect-trace --limit N` for large (live) traces.
- `docs/LIVE_RUNTIME.md` guide; Makefile targets `live-validate`, `live-demo`, `live-sim`, `bench-live`.
- Release automation: tag-triggered GitHub Release workflow with wheel and sdist artifacts.
- Governance docs: release process, community labels and quarterly themes, deprecation policy.
- Security: `SECURITY.md`, baseline `docs/THREAT_MODEL.md`, CycloneDX SBOM CI workflow and artifacts.
- Issue template for proposing **good first issue** work.
- `make test` Makefile target for pytest via the project virtualenv.
- `.editorconfig` for shared indentation and newline defaults.
- `robot-core version` CLI command and `make cpp-smoke` for the C++ smoke binary.
- `docs/DEVELOPMENT.md` quick reference for Python and C++ local workflows.
- `examples/robot_packs/README.md` index for graph YAML packs.
- `make graph-example` runs `examples/robot_packs/surveillance.yaml` into `logs/graph_example_trace.jsonl`.
- `deploy/README.md` overview for config, docker, systemd, and scripts.
- `make compile` byte-compiles `robot_core` and `shuttle`; `tests/test_bytecompile.py` guards syntax in CI.
- `benchmarks/README.md` index for runtime benchmark and deterministic replay tooling.
- `make check` runs `make test` and `make compile` as a pre-push gate.
- `tests/test_example_graph_packs.py` covers `examples/robot_packs/surveillance.yaml` wiring.
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1).
- `docs/README.md` documentation index for all guides.
- `schemas/README.md` and `shuttle/README.md` module overviews.
- `make replay-check` and `make clean-logs` Makefile targets.
- `nervlynx version` reads package version from metadata; `tests/test_nervlynx_cli_version.py`.
- Graph config validation: `validate_graph_config()`, `robot-core graph-validate`, and `make graph-validate`.
- `tests/test_graph_validation.py` validates structure and plugin existence for graph configs.
- Invalid graph fixture and CLI coverage for failure paths (`tests/fixtures/graph/invalid_missing_nodes.yaml`).
- `make preflight` bundles graph validation, replay check, and local checks.
- `make graph-validate-core` validates surveillance, delivery, and warehouse example packs.
- `tests/test_example_graph_packs.py` now covers `delivery.yaml` and `warehouse.yaml` too.
- `robot-core graph-validate` now accepts one or many config paths in one invocation.
- CLI tests cover mixed-validity multi-config graph validation behavior.
- CI now runs `robot-core graph-validate-core` on Python 3.11 before graph execution.
- CLI tests now cover `graph-validate` no-argument usage failure.
- Invalid fixture coverage for malformed `input_topics` (`tests/fixtures/graph/invalid_input_topics.yaml`).
- `make graph-run-core` executes bundled core packs and writes trace files to `logs/`.
- `robot-core graph-run-core --output-dir <dir>` executes bundled core packs in one CLI call.
- CI now runs `robot-core graph-run-core --output-dir logs/core_graph_runs` and uploads resulting traces as artifacts.
- `make graph-smoke` bundles core graph validation and execution locally.
- `tests/test_cli_graph_run_core.py` now verifies summary output and per-trace event counts.
- `robot-core graph-list-core` and `make graph-list-core` list bundled core graph config paths.
- `robot-core graph-list-core --format json` and `make graph-list-core-json` for machine-readable core pack discovery.
- `make graph-validate-file GRAPH=<path>` and `make graph-run-file GRAPH=<path> GRAPH_OUTPUT=<path>` for custom graph iteration.
- `robot-core graph-list-core --verify-exists` and `make graph-list-core-verify` for fast missing-pack detection.
- CI now runs `robot-core graph-list-core --verify-exists` on Python 3.11 to fail fast on missing core pack files.
- `make graph-list-core-verify-json` and JSON success coverage for `graph-list-core --verify-exists --format json`.
- `robot-core graph-doctor` and `make graph-doctor` verify core pack files exist and validate their configs in one call.

### Changed

- **`backend: auto`** picks gpiozero on a Raspberry Pi, falls back to RPi.GPIO only on boards it supports (never the Pi 5 family, whose GPIO sits behind RP1), and uses mock pins off the Pi, so one config runs in simulation and on the robot. `robot_core.hardware.detect_board()` reads the device-tree model; the drive node reports the resolved backend in `/stats` and as an info fault. The Pi setup guide now recommends `python3-gpiozero python3-lgpio`.
- **Core install is PyYAML + Typer only.** `pyzmq` and `pycapnp` moved to extras (`nervlynx[zmq]` for `ZmqJsonTransport`, `nervlynx[shuttle]` for the shuttle stack; `dev` still installs both), so installing on a Pi Zero 2 W compiles nothing. Missing extras now fail with the `pip install` hint instead of a bare `ModuleNotFoundError`. A `minimal-install` CI job runs the suite and a strict rover simulation without extras.
- `MetricsRegistry` is thread-safe and supports labels, histograms (Prometheus buckets plus reservoir quantiles), `# HELP` text, and JSON snapshots; unlabelled series render exactly as before. `serve_metrics` uses a threading server.
- `SystemClock.sleep_until_ns` compensates for OS sleep overshoot (learned per clock), cutting median tick lateness from milliseconds to microseconds on macOS; `Clock` gains `sleep_until_ns` and `simulated`.
- `PipelineRuntime.subscriptions` exposes topic subscribers; the dashboard no longer reads private runtime state.
- `ROADMAP.md` M4 developer-experience milestones marked complete where shipped.
- `CONTRIBUTING.md` local checks use `make test` and `robot-core` entry points; `docs/GETTING_STARTED.md` cross-links related docs.
- `README.md` shows a main-branch CI status badge and links `docs/DEVELOPMENT.md`; Common Commands lists `robot-core version` and the robot packs README.
- `CONTRIBUTING.md` mentions optional `make graph-example`.
- `docs/ARCHITECTURE.md` links threat model, development, and release documentation.
- `docs/DEVELOPMENT.md` documents `make compile` for quick syntax checks without pytest.
- `docs/DEVELOPMENT.md` documents `make check` and links `benchmarks/README.md`.
- `CONTRIBUTING.md` recommends `make check` for local validation.
- `README.md` links code of conduct and benchmarks documentation.
- `README.md` links `docs/README.md` and `shuttle/README.md`.
- `docs/DEVELOPMENT.md` documents `make replay-check`.
- `README.md` and `CONTRIBUTING.md` include graph validation commands.
- `docs/DEVELOPMENT.md` and `CONTRIBUTING.md` include the `make preflight` flow.
- `docs/DEVELOPMENT.md`, `CONTRIBUTING.md`, and `examples/robot_packs/README.md` document `make graph-validate-core`.
- `make graph-validate-core` now validates all core pack files in a single CLI call.
- `make preflight` now uses `graph-validate-core` (not single-file `graph-validate`).
- `README.md` Common Commands now includes `robot-core graph-validate-core`.
- Development/contributing and robot-pack docs include `make graph-run-core`.
- `make graph-run-core` now delegates to `robot-core graph-run-core --output-dir logs`.
- Development/contributing docs include `make graph-smoke`.
- README/development/robot-pack docs now include core graph list command usage.
- Core graph list docs now include JSON output usage for scripting.
- Docs now include parameterized graph Make targets for non-core packs.
- Graph list docs now include existence verification usage for core pack files.
- `make preflight` and docs now include core graph existence verification in the local gate.
- Docs now include JSON verify usage for machine-readable graph existence checks.
- Development/contributing and robot-pack docs include `graph-doctor` health-check usage.

### Fixed

- Chaos fault injection is now deterministic across processes. It seeded its RNG with the builtin `hash()` of payload keys, which Python randomises per process, so `chaos-pass` could report different results for the same settings (for example 0 vs 3 messages at 0.2/0.2).
- CLI tests work with Typer 0.27+, whose `CliRunner` no longer depends on Click (restores green CI on 3.10–3.12).

## [0.2.0] - 2026-04-15

### Added

- Production-confidence CI: Python 3.10–3.12 matrix, deterministic replay fixtures, benchmark baseline regression checks, artifact uploads.
- Support matrix (`docs/SUPPORT_MATRIX.md`) and API stability policy (`docs/API_STABILITY.md`) with `robot_core.stable_api` surface.
- Quickstart: `Makefile` targets (`make demo`), persona docs (`docs/GETTING_STARTED.md`), README demo GIF.
- Plugin ergonomics: `nervlynx init` scaffolder, `docs/PLUGIN_AUTHORING.md`, reference plugins and example graph packs (`examples/robot_packs/reference_*.yaml`).
- Benchmark baselines under `benchmarks/baselines/`.

### Changed

- `build_reference_runtime` accepts optional `clock` for deterministic tests.

### Fixed

- CI benchmark baseline tuned for GitHub-hosted runner throughput variance.

[Unreleased]: https://github.com/vedantparnaik/nervlynx/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/vedantparnaik/nervlynx/compare/v0.1.0...v0.2.0
