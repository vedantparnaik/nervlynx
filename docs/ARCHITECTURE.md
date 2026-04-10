# NervLynx Architecture

## Runtime layers

1. **Contracts**
   - Defines topic payload requirements and type expectations.
   - Supports compatibility checks for schema evolution.

2. **Runtime**
   - `PipelineRuntime`: deterministic, priority-aware in-process executor.
   - `AsyncPipelineRuntime`: async event-loop variant for concurrent pipelines.
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

7. **Debugging**
   - JSONL trace recording and replay.
   - Trace timeline, per-topic latency, and end-to-end flow stats.

## Dataflow model

```
Sensor Ingest -> Perception/Fusion -> Planning -> Actuation -> Uplink/Alert
```

All messages carry a `trace_id` so cross-stage diagnostics are straightforward.
