# NervLynx

NervLynx is an open robotics runtime backbone for reliable, traceable robot pipelines.
It is designed to help teams move from prototype scripts to a structured runtime that is debuggable, testable, and deployment-friendly.

The repository includes:
- `robot_core`: generic, reusable runtime for Sensor Ingest -> Perception/Fusion -> Planning/Action flows
- `shuttle`: a fixed-route shuttle reference stack built on the same patterns

## Core capabilities

- Priority-aware runtime scheduling with queue backpressure detection
- Sync + async runtimes with injectable clocks (`SystemClock`, `SimulatedClock`)
- Transport abstraction (`InMemoryTransport`, `ZmqJsonTransport`) for local or multi-process flows
- Typed runtime envelopes: `topic`, `source`, `sequence`, `timestamp`, `schema`, `trace_id`
- Contract validation and migration checks for topic evolution
- Plugin SDK + entry point discovery + config-driven graph wiring
- Watchdog liveness checks, supervisor lifecycle orchestration
- Observability helpers (trace timelines, per-topic latency, end-to-end flow stats)
- Prometheus-style live metrics endpoint
- JSONL recorder/replay with binary payload support
- Surveillance smoke test + failure-mode smoke matrix
- Python and C++ reference runtimes with CI

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
```

## Run examples

Generic runtime example:

```bash
robot-core run-example --output logs/robot_core_trace.jsonl
robot-core replay logs/robot_core_trace.jsonl
```

Surveillance smoke:

```bash
robot-core smoke-surveillance --output logs/smoke_surveillance_trace.jsonl
```

Failure-mode smoke matrix (GPS loss, camera drop, IMU fault, low Wi-Fi):

```bash
robot-core smoke-matrix --output-dir logs/smoke_matrix
```

Trace observability:

```bash
robot-core inspect-trace logs/smoke_surveillance_trace.jsonl
```

Supervisor and metrics demos:

```bash
robot-core supervisor-demo
robot-core serve-metrics --duration-s 5 --port 9108
```

Config-driven graph run:

```bash
robot-core run-graph deploy/config/graph_surveillance.yaml --output logs/graph_trace.jsonl
```

C++ core smoke test:

```bash
cmake -S cpp_core -B cpp_core/build
cmake --build cpp_core/build
./cpp_core/build/smoke_surveillance
ctest --test-dir cpp_core/build --output-on-failure
```

Run all tests:

```bash
pytest -q
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
- `docs/`: architecture and design notes

## Extending to your robot

1. Add adapters for your sensors (multi-camera, GPS, IMU, LiDAR, mic, etc.)
2. Define topic contracts (fields and types)
3. Implement node plugins and wire a graph via YAML config
4. Set watchdog thresholds and supervisor dependencies
5. Record traces on every run and replay regressions deterministically
6. Export live metrics and connect your monitoring stack

## Notes

- This project is intended as a practical foundation, not a full production autonomy stack.
- Safety and deployment decisions should be adapted to your platform and compliance requirements.
