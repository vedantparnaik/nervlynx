# Getting Started Paths

This guide gives a fast "first successful run" for different personas.

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
