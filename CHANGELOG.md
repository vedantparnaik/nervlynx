# Changelog

All notable changes to NervLynx are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Release automation: tag-triggered GitHub Release workflow with wheel and sdist artifacts.
- Governance docs: release process, community labels and quarterly themes, deprecation policy.
- Security: `SECURITY.md`, baseline `docs/THREAT_MODEL.md`, CycloneDX SBOM CI workflow and artifacts.
- Issue template for proposing **good first issue** work.
- `make test` Makefile target for pytest via the project virtualenv.
- `.editorconfig` for shared indentation and newline defaults.
- `robot-core version` CLI command and `make cpp-smoke` for the C++ smoke binary.
- `docs/DEVELOPMENT.md` quick reference for Python and C++ local workflows.
- `examples/robot_packs/README.md` index for graph YAML packs.
- `make graph-example` runs `examples/robot_packs/surveillance.yaml` into `logs/graph_example_trace.jsonl`.
- `deploy/README.md` overview for config, docker, systemd, and scripts.
- `make compile` byte-compiles `robot_core` and `shuttle`; `tests/test_bytecompile.py` guards syntax in CI.
- `benchmarks/README.md` index for runtime benchmark and deterministic replay tooling.
- `make check` runs `make test` and `make compile` as a pre-push gate.
- `tests/test_example_graph_packs.py` covers `examples/robot_packs/surveillance.yaml` wiring.
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1).
- `docs/README.md` documentation index for all guides.
- `schemas/README.md` and `shuttle/README.md` module overviews.
- `make replay-check` and `make clean-logs` Makefile targets.
- `nervlynx version` reads package version from metadata; `tests/test_nervlynx_cli_version.py`.
- Graph config validation: `validate_graph_config()`, `robot-core graph-validate`, and `make graph-validate`.
- `tests/test_graph_validation.py` validates structure and plugin existence for graph configs.
- Invalid graph fixture and CLI coverage for failure paths (`tests/fixtures/graph/invalid_missing_nodes.yaml`).
- `make preflight` bundles graph validation, replay check, and local checks.
- `make graph-validate-core` validates surveillance, delivery, and warehouse example packs.
- `tests/test_example_graph_packs.py` now covers `delivery.yaml` and `warehouse.yaml` too.
- `robot-core graph-validate` now accepts one or many config paths in one invocation.
- CLI tests cover mixed-validity multi-config graph validation behavior.
- CI now runs `robot-core graph-validate-core` on Python 3.11 before graph execution.
- CLI tests now cover `graph-validate` no-argument usage failure.
- Invalid fixture coverage for malformed `input_topics` (`tests/fixtures/graph/invalid_input_topics.yaml`).
- `make graph-run-core` executes bundled core packs and writes trace files to `logs/`.
- `robot-core graph-run-core --output-dir <dir>` executes bundled core packs in one CLI call.
- CI now runs `robot-core graph-run-core --output-dir logs/core_graph_runs` and uploads resulting traces as artifacts.
- `make graph-smoke` bundles core graph validation and execution locally.
- `tests/test_cli_graph_run_core.py` now verifies summary output and per-trace event counts.
- `robot-core graph-list-core` and `make graph-list-core` list bundled core graph config paths.
- `robot-core graph-list-core --format json` and `make graph-list-core-json` for machine-readable core pack discovery.
- `make graph-validate-file GRAPH=<path>` and `make graph-run-file GRAPH=<path> GRAPH_OUTPUT=<path>` for custom graph iteration.
- `robot-core graph-list-core --verify-exists` and `make graph-list-core-verify` for fast missing-pack detection.

### Changed

- `ROADMAP.md` M4 developer-experience milestones marked complete where shipped.
- `CONTRIBUTING.md` local checks use `make test` and `robot-core` entry points; `docs/GETTING_STARTED.md` cross-links related docs.
- `README.md` shows a main-branch CI status badge and links `docs/DEVELOPMENT.md`; Common Commands lists `robot-core version` and the robot packs README.
- `CONTRIBUTING.md` mentions optional `make graph-example`.
- `docs/ARCHITECTURE.md` links threat model, development, and release documentation.
- `docs/DEVELOPMENT.md` documents `make compile` for quick syntax checks without pytest.
- `docs/DEVELOPMENT.md` documents `make check` and links `benchmarks/README.md`.
- `CONTRIBUTING.md` recommends `make check` for local validation.
- `README.md` links code of conduct and benchmarks documentation.
- `README.md` links `docs/README.md` and `shuttle/README.md`.
- `docs/DEVELOPMENT.md` documents `make replay-check`.
- `README.md` and `CONTRIBUTING.md` include graph validation commands.
- `docs/DEVELOPMENT.md` and `CONTRIBUTING.md` include the `make preflight` flow.
- `docs/DEVELOPMENT.md`, `CONTRIBUTING.md`, and `examples/robot_packs/README.md` document `make graph-validate-core`.
- `make graph-validate-core` now validates all core pack files in a single CLI call.
- `make preflight` now uses `graph-validate-core` (not single-file `graph-validate`).
- `README.md` Common Commands now includes `robot-core graph-validate-core`.
- Development/contributing and robot-pack docs include `make graph-run-core`.
- `make graph-run-core` now delegates to `robot-core graph-run-core --output-dir logs`.
- Development/contributing docs include `make graph-smoke`.
- README/development/robot-pack docs now include core graph list command usage.
- Core graph list docs now include JSON output usage for scripting.
- Docs now include parameterized graph Make targets for non-core packs.
- Graph list docs now include existence verification usage for core pack files.

## [0.2.0] - 2026-04-15

### Added

- Production-confidence CI: Python 3.10–3.12 matrix, deterministic replay fixtures, benchmark baseline regression checks, artifact uploads.
- Support matrix (`docs/SUPPORT_MATRIX.md`) and API stability policy (`docs/API_STABILITY.md`) with `robot_core.stable_api` surface.
- Quickstart: `Makefile` targets (`make demo`), persona docs (`docs/GETTING_STARTED.md`), README demo GIF.
- Plugin ergonomics: `nervlynx init` scaffolder, `docs/PLUGIN_AUTHORING.md`, reference plugins and example graph packs (`examples/robot_packs/reference_*.yaml`).
- Benchmark baselines under `benchmarks/baselines/`.

### Changed

- `build_reference_runtime` accepts optional `clock` for deterministic tests.

### Fixed

- CI benchmark baseline tuned for GitHub-hosted runner throughput variance.

[Unreleased]: https://github.com/vedantparnaik/nervlynx/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/vedantparnaik/nervlynx/compare/v0.1.0...v0.2.0
