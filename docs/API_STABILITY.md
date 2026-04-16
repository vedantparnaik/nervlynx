# API Stability (v0.x)

NervLynx exposes a **minimal stable API surface** for users building integrations during v0.x.

## Stability Guarantees

- Imports from `robot_core.stable_api` will remain backwards compatible within the v0.x line.
- Symbols may be added, but existing symbol names and behavior contracts are preserved unless a security or correctness issue requires urgent change.
- If a breaking change is unavoidable, it will be documented in `README.md`, `CHANGELOG.md`, and release notes with a migration note. See also `docs/DEPRECATION_POLICY.md`.

## Stable Surface

Use these symbols from `robot_core.stable_api`:

- Runtime: `Envelope`, `RuntimeMessage`, `PipelineRuntime`, `SystemClock`, `SimulatedClock`
- Safety: `HealthWatchdog`
- Plugins: `PluginRegistry`, `SensorPlugin`, `NodePlugin`
- Recording: `write_jsonl`, `read_jsonl`
- Metrics: `MetricsRegistry`, `serve_metrics`

## Recommendation

For production integrations, import from `robot_core.stable_api` instead of deeper module paths.
This reduces upgrade risk across minor versions during fast iteration.
