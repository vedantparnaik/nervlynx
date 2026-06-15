# Development tips

Shortcuts for day-to-day work on NervLynx. For full policy and PR expectations see `CONTRIBUTING.md`.

## Python

```bash
make setup    # once: venv + editable install
make test     # pytest
make check    # test + compile (handy before push)
make preflight  # core graph existence + validation + replay check + full local gate
make graph-smoke  # core graph validation + core graph execution bundle
make graph-validate  # validate surveillance graph structure and plugin refs
make graph-validate-core  # validate bundled surveillance/delivery/warehouse packs
make graph-validate-file GRAPH=examples/robot_packs/warehouse.yaml  # validate any graph file
make graph-list-core  # print bundled core graph config paths
make graph-list-core-json  # print bundled core graph config paths as JSON
make graph-list-core-verify  # fail fast if bundled core graph files are missing
make graph-list-core-verify-json  # verify bundled core graph files and print JSON
make graph-run-core  # execute bundled core packs and emit logs/*_trace.jsonl
make graph-run-file GRAPH=examples/robot_packs/warehouse.yaml GRAPH_OUTPUT=logs/warehouse_trace.jsonl
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
- Make targets `graph-validate-file` and `graph-run-file` let you pass custom graph paths via `GRAPH=...`.
- Core pack discovery supports text and JSON formats via `robot-core graph-list-core --format <text|json>`.
- Add `--verify-exists` to make graph discovery fail if a bundled config path goes missing.
- Use `--verify-exists --format json` for machine-readable CI/script checks.
- `make preflight` includes the core graph existence check before validation and tests.
- `graph-run-core` is also available directly: `robot-core graph-run-core --output-dir logs`.
