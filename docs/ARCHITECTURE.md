# NervLynx Architecture

## Runtime layers

1. **Contracts**
   - Defines topic payload requirements and type expectations.
   - Supports compatibility checks for schema evolution.

2. **Runtime**
   - `PipelineRuntime`: deterministic, priority-aware in-process executor (one seed, run to completion).
   - `AsyncPipelineRuntime`: async event-loop variant for concurrent pipelines.
   - `LiveRuntime`: continuous executor with fixed-rate ticks, real or simulated clock, circuit breakers, watchdog, latched e-stop, and a stall guard (`docs/LIVE_RUNTIME.md`).
   - Queue backpressure is surfaced as runtime faults.

3. **Transport**
   - `InMemoryTransport`: low-latency test transport.
   - `ZmqJsonTransport`: local multi-process transport using ZeroMQ.

4. **Plugins and Graph**
   - Sensor and node plugins are discoverable via entry points.
   - Graph wiring is config-driven through YAML.

5. **Operations**
   - `RuntimeSupervisor`: dependency-aware startup/shutdown ordering.
   - `HealthWatchdog`: stale-node liveness fault detection.
   - `CheckpointStore`: persistent node state snapshots for recovery.
   - `ChaosConfig` + chaos runner: controlled fault injection for resilience testing.
   - Metrics endpoint for Prometheus scraping.
   - Dashboard endpoint for `/health`, `/graph`, and `/stats`.

6. **Security**
   - Payload signing and verification via HMAC.
   - Topic-level access policy checks for publish/subscribe boundaries.
   - Live HTTP control gated by `--allow-control`, a token, and a topic allowlist; e-stop always allowed.

7. **Debugging**
   - JSONL trace recording and replay (one-shot runs and streaming live sessions).
   - Trace timeline, per-topic latency, and end-to-end flow stats.
   - Per-session run reports with tick jitter, handler cost, and trace latency percentiles.

8. **Hardware and simulation**
   - Pin backends (`mock`, `rpi_gpio`, `gpiozero`) and BTS7960/TB6612 motor drivers.
   - `SkidSteerDrive` actuator node (deadman, kick, slew, floors) and a `SkidSteerSim` plant.

## Live control loop

```
scripted_drive / HTTP teleop --cmd.drive--> skid_steer_drive --PWM--> motors
                                                 |
                                            drive.state --> skid_steer_sim --> odom
```

The stall guard, watchdog, and e-stop sit outside this path and can stop the motors
regardless of what the nodes are doing.

## Related documentation

- Trust boundaries and deployment scope: `docs/THREAT_MODEL.md`
- Contributor and local workflows: `docs/DEVELOPMENT.md`
- Release tagging and artifacts: `docs/RELEASE_PROCESS.md`

## Dataflow model

```
Sensor Ingest -> Perception/Fusion -> Planning -> Actuation -> Uplink/Alert
```

All messages carry a `trace_id` so cross-stage diagnostics are straightforward.
