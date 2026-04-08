# Shuttle Stack (Weeks 1-4 Foundation)

This repository contains a minimal, production-minded ADAS research foundation for a fixed-route shuttle.

## Included in this build

- Cap'n Proto message schema (`schemas/shuttle.capnp`)
- ZeroMQ topic pub/sub transport
- Service registry with expected rates and criticality
- Monotonic message headers with sequence numbers
- Heartbeat + watchdog fault detector
- Safety state machine process (`normal`, `caution`, `mrs`)
- Multi-process pipeline:
  - `state_estimator`
  - `route_manager`
  - `planner`
  - `controller`
  - `safety_manager`
- Logger and replay tools for offline debugging
- Sensor adapter interfaces with `synthetic` and `replay` modes
- Route profile config for teach-and-repeat style operation (`configs/route_loop.yaml`)

## Quick start

1. Install dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -U pip
   pip install -e .
   ```

2. Run one process per terminal:

   ```bash
   shuttle-stack run-broker
   shuttle-stack run-state
   shuttle-stack run-route-manager --route configs/route_loop.yaml
   shuttle-stack run-planner
   shuttle-stack run-controller
   shuttle-stack run-watchdog
   shuttle-stack run-safety-manager
   ```

3. Optional logging and replay:

   ```bash
   shuttle-stack run-logger --output logs/session.jsonl
   shuttle-stack replay --input logs/session.jsonl --mode realtime
   ```

## ODD assumptions baked into this baseline

- Fixed route
- Daylight, dry weather
- Operator in loop
- Low-speed initial operation
- Minimal-risk-stop behavior handled by watchdog alerts and command gating hooks

## Notes

- This is a Week 1-4 software foundation. Real sensor drivers and drive-by-wire integration are intentionally not included yet.
- Cap'n Proto schema is versioned through `schemaVersion` fields and should be evolved with backward compatibility in mind.

## Testing

Install dev dependencies and run:

```bash
pip install -e ".[dev]"
pytest -q
```

## Robotics Core Skeleton (Reusable Beyond ADAS)

This repository now also includes a generic runtime skeleton in `robot_core/` for building non-vehicle robotics stacks.

### What it gives you

- Typed runtime envelopes with topic, source, sequence, timestamp, schema, and `trace_id`
- Pluggable topic graph (`PipelineRuntime`) for Sensor Ingest -> Perception/Fusion -> Planning/Action dataflow
- Node heartbeat tracking and a generic liveness watchdog (`HealthWatchdog`)
- JSONL record/replay helpers for deterministic debugging (`robot_core.recorder`)
- A runnable reference flow (`sensors.raw` -> `perception.state` -> `control.intent`)

### Quick try

```bash
pip install -e ".[dev]"
robot-core run-example --output logs/robot_core_trace.jsonl
robot-core replay logs/robot_core_trace.jsonl
```

### How to extend for your robot

1. Replace the example producer with your adapters (N cameras, GPS, IMU, LiDAR, etc.)
2. Add processing nodes that subscribe/publish the topics your stack needs
3. Define watchdog thresholds per critical node/topic
4. Record every run and replay traces when regressions show up

This keeps the hard parts centralized: graph wiring, traceable dataflow, and post-failure debugging.
