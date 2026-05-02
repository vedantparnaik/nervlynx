# Development tips

Shortcuts for day-to-day work on NervLynx. For full policy and PR expectations see `CONTRIBUTING.md`.

## Python

```bash
make setup    # once: venv + editable install
make test     # pytest
make demo     # install, run-example, replay
make graph-example  # run-graph on examples/robot_packs/surveillance.yaml
robot-core version
```

## C++ reference runtime

```bash
make cpp-smoke
# or manually:
cmake -S cpp_core -B cpp_core/build && cmake --build cpp_core/build
ctest --test-dir cpp_core/build --output-on-failure
```

## Useful paths

- Core runtime and CLI: `robot_core/`
- Graph examples: `examples/robot_packs/`, `deploy/config/`
- CI workflows: `.github/workflows/`
