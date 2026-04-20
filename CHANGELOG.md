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

### Changed

- `ROADMAP.md` M4 developer-experience milestones marked complete where shipped.
- `CONTRIBUTING.md` local checks use `make test` and `robot-core` entry points; `docs/GETTING_STARTED.md` cross-links related docs.

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
