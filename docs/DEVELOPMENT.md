# Development tips

Shortcuts for day-to-day work on NervLynx. For full policy and PR expectations see `CONTRIBUTING.md`.

## Python

```bash
make setup    # once: venv + editable install
make test     # pytest
make check    # test + compile (handy before push)
make preflight  # core graph validation + replay check + full local gate
make graph-validate  # validate surveillance graph structure and plugin refs
make graph-validate-core  # validate bundled surveillance/delivery/warehouse packs
make graph-run-core  # execute bundled core packs and emit logs/*_trace.jsonl
make replay-check  # deterministic replay fixture (matches CI)
make demo     # install, run-example, replay
make compile  # quick syntax check without pytest
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
- Benchmarks and replay checks: `benchmarks/README.md`
- Graph validation supports one or many config paths in a single command; `graph-validate-core` is the fast path for bundled packs.
