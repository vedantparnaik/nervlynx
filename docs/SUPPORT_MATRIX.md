# Support Matrix

NervLynx support levels are organized by platform and validation depth.

## Runtime Compatibility

| Dimension | Supported | CI Coverage | Notes |
| --- | --- | --- | --- |
| Python | 3.10, 3.11, 3.12 | Yes | Core tests run on all listed versions. |
| OS | Ubuntu 24.04 (GitHub runner), macOS 14+, Windows 11+ (best effort) | Ubuntu (full), others (community validation) | Use Python 3.11 for strongest parity with CI. |
| CPU arch | x86_64, arm64 | x86_64 in CI, arm64 via edge scripts | arm64 is intended for edge targets using deploy scripts. |

## Hardware Target Tiers

| Tier | Examples | Status | Guidance |
| --- | --- | --- | --- |
| Tier 1: CI reference | GitHub-hosted Linux VM (x86_64) | Fully validated | Use for release gates and reproducible checks. |
| Tier 2: Developer workstation | macOS/Linux laptops | Supported | Best for local development and smoke checks. |
| Tier 3: Edge deployment | Raspberry Pi class ARM64 / industrial x86 mini-PC | Supported with deployment validation | Validate sensor adapters, networking, and watchdog tuning per robot. |

## Validation Expectations

- Run `make demo` for first-run confidence.
- Run `pytest -q` plus `robot-core smoke-matrix` before shipping runtime changes.
- Keep deterministic replay and benchmark baseline checks green in CI before release tagging.
