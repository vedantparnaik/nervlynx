# Contributing to NervLynx

Thanks for contributing.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
```

## Local checks

```bash
pytest -q
cmake -S cpp_core -B cpp_core/build
cmake --build cpp_core/build
ctest --test-dir cpp_core/build --output-on-failure
python -m robot_core.cli smoke-matrix --output-dir logs/smoke_matrix
python -m robot_core.cli chaos-pass --drop-probability 0.1 --mutate-probability 0.1
python benchmarks/benchmark_runtime.py
```

## Contribution guidelines

- Keep changes small and focused.
- Add or update tests for behavior changes.
- Preserve backward compatibility for message schemas when possible.
- Document new topics, node contracts, and assumptions in `README.md`.

## Pull requests

- Use clear titles and describe motivation.
- Include a brief test plan and results.
- Link issues when applicable.

## Releases and changelog

- Every user-visible change should have an entry under `[Unreleased]` in `CHANGELOG.md` (or the maintainer adds it when merging).
- Version bumps and release tagging follow `docs/RELEASE_PROCESS.md`.
- Pushing a tag matching `v*.*.*` triggers the GitHub Release workflow and attaches built wheels and sdists.
