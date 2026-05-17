# Benchmarks and production-confidence checks

Scripts used locally and in CI to measure runtime performance and validate deterministic behavior.

| Script | Purpose |
| --- | --- |
| `benchmark_runtime.py` | Throughput benchmark for the reference pipeline (`--output-json`, `--baseline`). |
| `check_deterministic_replay.py` | Compare generated trace output against a fixture (`tests/fixtures/replay/`). |
| `baselines/` | Versioned JSON baselines for CI regression checks (see `baselines/README.md`). |

## Local usage

```bash
python benchmarks/benchmark_runtime.py --loops 5000
python benchmarks/check_deterministic_replay.py \
  --seed tests/fixtures/replay/seed_trace.jsonl \
  --expected tests/fixtures/replay/expected_trace.jsonl \
  --output logs/replay_actual_trace.jsonl
```

CI runs the benchmark baseline check and deterministic replay on Python 3.11 (see `.github/workflows/ci.yml`).
