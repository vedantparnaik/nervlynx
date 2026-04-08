# NervLynx

NervLynx is an open robotics runtime backbone for reliable, traceable robot pipelines.

It includes:
- `robot_core`: generic, reusable runtime for Sensor Ingest -> Perception/Fusion -> Planning/Action flows
- `shuttle`: a fixed-route shuttle reference stack built on the same patterns

## Core capabilities

- Typed runtime envelopes with `topic`, `source`, `sequence`, `timestamp`, `schema`, and `trace_id`
- Pluggable topic graph via `PipelineRuntime`
- Node heartbeat tracking and liveness checks via `HealthWatchdog`
- JSONL record/replay for deterministic debugging
- Smoke scenario for a surveillance robot (HQ telemetry + alert path)
- Failure-mode smoke matrix (GPS loss, camera drop, IMU fault, low Wi-Fi)
- Versioned topic contracts and migration checks
- Plugin SDK primitives for sensor and processing-node extension
- Basic observability helpers for per-trace timeline and topic latency stats

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
```

## Run examples

Generic core example:

```bash
robot-core run-example --output logs/robot_core_trace.jsonl
robot-core replay logs/robot_core_trace.jsonl
```

Surveillance smoke test:

```bash
robot-core smoke-surveillance --output logs/smoke_surveillance_trace.jsonl
pytest -q
```

Failure-mode smoke matrix:

```bash
robot-core smoke-matrix --output-dir logs/smoke_matrix
```

Trace observability:

```bash
robot-core inspect-trace logs/smoke_surveillance_trace.jsonl
```

C++ core smoke test:

```bash
cmake -S cpp_core -B cpp_core/build
cmake --build cpp_core/build
./cpp_core/build/smoke_surveillance
ctest --test-dir cpp_core/build --output-on-failure
```

Shuttle reference stack:

```bash
shuttle-stack run-broker
shuttle-stack run-state
shuttle-stack run-route-manager --route configs/route_loop.yaml
shuttle-stack run-planner
shuttle-stack run-controller
shuttle-stack run-watchdog
shuttle-stack run-safety-manager
```

## Repository layout

- `robot_core/`: reusable robotics runtime skeleton
- `shuttle/`: fixed-route shuttle reference implementation
- `configs/`: service rates and route profiles
- `schemas/`: Cap'n Proto schemas
- `tests/`: unit tests and smoke checks
- `deploy/`: deployment profiles (`systemd`, `docker`, runtime config)
- `.github/workflows/ci.yml`: Python + C++ CI checks

## Extending to your robot

1. Add adapters for your sensors (multi-camera, GPS, IMU, LiDAR, mic, etc.)
2. Define topic contracts and processing nodes
3. Set watchdog thresholds for critical paths
4. Record traces in every run and replay failures deterministically

## Notes

- This project is intended as a practical foundation, not a full production autonomy stack.
- Safety and deployment decisions should be adapted to your platform and compliance requirements.
