# NervLynx

[![10-minute demo](https://img.shields.io/badge/quickstart-10--minute%20demo-2ea44f)](#quick-start-10-minute-path)
[![getting started paths](https://img.shields.io/badge/docs-getting%20started-1f6feb)](docs/GETTING_STARTED.md)
[![ci](https://github.com/vedantparnaik/nervlynx/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/vedantparnaik/nervlynx/actions/workflows/ci.yml)

NervLynx is an open, modular robotics runtime framework for building reliable and observable robot pipelines.
It helps teams move from ad-hoc prototype scripts to production-style architecture with typed contracts, lifecycle control, traceability, and repeatable validation.

## Demo

![NervLynx quickstart terminal demo](docs/assets/nervlynx-demo.gif)

## Why NervLynx

- **Live robot runtime**: run graphs continuously with fixed-rate control loops, a live dashboard with teleop, and a trace plus report for every session (`robot-core run-live`)
- **Safety by default**: drive deadman, latched e-stop, liveness watchdog, per-node circuit breakers, and a stall guard that stops actuators if the executor hangs
- **Hardware ready**: RPi.GPIO / gpiozero backends, BTS7960 and TB6612 motor drivers, and a skid-steer drive node, all testable on a laptop with the mock backend and a simulated rover
- **Structured runtime**: deterministic and async execution modes with priority scheduling
- **Traceable dataflow**: envelope metadata (`topic`, `source`, `sequence`, `timestamp`, `schema`, `trace_id`)
- **Operational safety**: watchdog liveness checks, backpressure detection, startup dependency supervision, checkpoint recovery
- **Extensibility**: plugin SDK, entry-point discovery, and config-driven graph wiring
- **Observability first**: replayable traces, latency/flow stats, Prometheus-style metrics, and runtime dashboard endpoints
- **Deployment ready**: Python and C++ runtimes, CI workflows, deploy profiles, and edge install/config sync scripts
- **Security-ready baseline**: payload signing and topic access policy checks

## Architecture At A Glance

```text
Sensor Ingest -> Perception/Fusion -> Planning -> Actuation -> Uplink/Alerts
```

Primary modules:
- `robot_core`: reusable runtime primitives and CLI, including the live executor (`live.py`), drive and hardware layers (`drive.py`, `hardware.py`), simulation nodes (`sim.py`), and the live HTTP surface (`server.py`)
- `shuttle`: reference fixed-route stack built on the same patterns (`shuttle/README.md`)

## Quick Start (10-Minute Path)

```bash
make demo
```

`make demo` creates a local virtualenv, installs dependencies, runs a baseline runtime demo, and replays the produced trace.

If you prefer manual setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
robot-core run-example --output logs/robot_core_trace.jsonl
robot-core replay logs/robot_core_trace.jsonl
```

## Run a Live Robot Graph

```bash
make setup
robot-core run-live examples/live/rover_sim.yaml --duration-s 30 --allow-control
```

Open `http://127.0.0.1:9120/` to watch nodes, topics, latencies, and faults update live,
drive with W/A/S/D, and hit **E-STOP**. The simulated rover needs no hardware: a scripted
driver commands a skid-steer drive on mock motors and a kinematic model produces
odometry. When the run ends, a report (tick jitter, command-to-actuation latency, safety
events) is printed and saved with the full message trace under `logs/live/`.

On a Raspberry Pi robot, point it at real pins:

```bash
robot-core live-validate examples/live/rover_bts7960.yaml     # no hardware touched
robot-core run-live examples/live/rover_bts7960.yaml --host 0.0.0.0 --allow-control
robot-core top http://<pi-address>:9120                       # terminal view over SSH
```

Details, the YAML reference, the safety model, and the metrics catalogue are in
`docs/LIVE_RUNTIME.md`.

## Quick Start by Persona

- **Robotics engineer**: run + inspect dataflow quickly (`make demo` then `robot-core inspect-trace ...`)
- **Platform engineer**: bootstrap environment and validate baseline checks (`make setup`, `pytest -q`)
- **Student / hobbyist**: run the easiest happy-path, then try smoke + replay loops

Detailed paths: `docs/GETTING_STARTED.md`. Full doc index: `docs/README.md`.

## Production Confidence

- Compatibility and support tiers: `docs/SUPPORT_MATRIX.md`
- API compatibility guarantees: `docs/API_STABILITY.md`
- Release benchmark baselines: `benchmarks/baselines/`
- Deterministic replay fixture for CI: `tests/fixtures/replay/`
- Benchmark scripts and baselines: `benchmarks/README.md`
- Deprecation and versioning policy: `docs/DEPRECATION_POLICY.md`
- Release history: `CHANGELOG.md`
- How we ship versions: `docs/RELEASE_PROCESS.md`
- Security reporting and supported versions: `SECURITY.md`
- Trust boundaries (baseline): `docs/THREAT_MODEL.md`

## Contributing

- Contributor guide: `CONTRIBUTING.md`
- Code of conduct: `CODE_OF_CONDUCT.md`
- Day-to-day dev shortcuts: `docs/DEVELOPMENT.md`
- Labels and quarterly themes: `docs/COMMUNITY.md`
- Roadmap and milestones: `ROADMAP.md`

## Plugin Ergonomics

- Scaffold a new plugin pack with `nervlynx init <your-pack-name>`
- Authoring guide: `docs/PLUGIN_AUTHORING.md`
- Reference graph packs: `examples/robot_packs/reference_*.yaml`
- Pack index and one-liners: `examples/robot_packs/README.md`

## Common Commands

```bash
# Installed package version
robot-core version

# Live graphs: continuous execution, dashboard, run reports
robot-core live-validate examples/live/*.yaml
robot-core run-live examples/live/rover_sim.yaml --duration-s 30
robot-core run-live examples/live/rover_sim.yaml --sim-time --duration-s 600 --strict
robot-core top http://127.0.0.1:9120
python benchmarks/benchmark_live.py

# Basic runtime demo
robot-core run-example --output logs/robot_core_trace.jsonl
robot-core replay logs/robot_core_trace.jsonl

# Surveillance smoke and failure matrix
robot-core smoke-surveillance --output logs/smoke_surveillance_trace.jsonl
robot-core smoke-matrix --output-dir logs/smoke_matrix

# Trace and contracts tooling
robot-core inspect-trace logs/smoke_surveillance_trace.jsonl
robot-core contracts-check
robot-core chaos-pass --drop-probability 0.2 --mutate-probability 0.2
robot-core chaos-pass --drop-probability 0.2 --mutate-probability 0.2 --trials 1000

# Supervisor and metrics demos
robot-core supervisor-demo
robot-core serve-metrics --duration-s 5 --port 9108
robot-core dashboard-demo --duration-s 5 --port 9120

# Config-driven graph run
robot-core graph-list-core
robot-core graph-list-core --format json
robot-core graph-list-core --verify-exists
robot-core graph-list-core --verify-exists --format json
robot-core graph-doctor
robot-core graph-validate deploy/config/graph_surveillance.yaml
robot-core graph-validate-core
robot-core run-graph deploy/config/graph_surveillance.yaml --output logs/graph_trace.jsonl
robot-core graph-run-core --output-dir logs
make graph-validate-file GRAPH=examples/robot_packs/warehouse.yaml
make graph-run-file GRAPH=examples/robot_packs/warehouse.yaml GRAPH_OUTPUT=logs/warehouse_trace.jsonl

# Distributed node mode over transport (local demo)
robot-core distributed-demo

# Checkpoint persistence demo
robot-core checkpoint-demo --node-name planner
```

## C++ Runtime Smoke Test

```bash
cmake -S cpp_core -B cpp_core/build
cmake --build cpp_core/build
./cpp_core/build/smoke_surveillance
ctest --test-dir cpp_core/build --output-on-failure
```

## Validation

```bash
pytest -q
python benchmarks/benchmark_runtime.py
python benchmarks/benchmark_live.py
```

CI executes Python tests, smoke matrix, contracts checks, graph runs, a strict 30 s live rover simulation, benchmarks, and C++ build/smoke checks on push and pull requests.

## Repository Layout

- `robot_core/`: core runtime, contracts, transport, observability, metrics, CLI
- `cpp_core/`: C++ runtime reference implementation and smoke executable
- `shuttle/`: reference application stack
- `tests/`: Python unit and integration smoke tests
- `deploy/`: deployment profiles (`systemd`, `docker`, config`)
- `deploy/scripts/`: edge install and config sync scripts
- `examples/robot_packs/`: reusable robot profile graph configs (one-shot)
- `examples/live/`: live graphs: simulated rover, BTS7960 and TB6612 rovers, continuous surveillance
- `benchmarks/`: runtime performance benchmarks
- `.github/workflows/`: CI pipeline definitions
- `.github/ISSUE_TEMPLATE/`: bug/feature templates for contributors
- `docs/`: architecture and design notes
- `ROADMAP.md`: v0.2 milestones and good-first-issues

## Extending For Your Robot

1. Add sensor adapters and normalize payloads.
2. Define/validate topic contracts with field types.
3. Implement node plugins and wire graphs via YAML.
4. Set watchdog and supervisor policies for your runtime.
5. Enable trace recording and replay in all test environments.
6. Export metrics to your monitoring platform.
7. Use checkpoints for recovery and run chaos/benchmark passes regularly.

## Deployment Shortcuts

```bash
# One-command edge install (target defaults to /opt/nervlynx)
bash deploy/scripts/install_edge.sh

# Sync graph config to a target directory
python deploy/scripts/sync_config.py deploy/config/graph_surveillance.yaml /tmp/nervlynx-config
```

Deploy layout reference: `deploy/README.md`.

## Project Scope 

NervLynx is a robust runtime foundation, not a complete end-product autonomy system.
Production deployment decisions (safety, compliance, networking, and hardware integration) should be validated for your operational environment.
