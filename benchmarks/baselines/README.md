# Benchmark Baselines

This directory stores release-tagged benchmark baselines used by CI regression checks.

## Update Process (per release)

1. Run the benchmark command:

```bash
PYTHONPATH=. python benchmarks/benchmark_runtime.py --loops 5000 --output-json logs/benchmark_runtime.json
```

2. Copy the generated metrics into a new versioned baseline file (for example `reference_runtime_v0.3.0.json`).
3. Update the CI baseline path in `.github/workflows/ci.yml`.
4. Keep prior baseline files for historical trend comparisons.
