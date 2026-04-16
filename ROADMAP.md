# NervLynx v0.2 Roadmap

## Goals

- Mature distributed runtime mode and multi-node deployment ergonomics.
- Strengthen schema governance across Python and C++ implementations.
- Improve observability, resilience, and security posture for real deployments.

## Quarterly focus (2026)

| Quarter | Focus |
| --- | --- |
| **Q2** | Release automation, changelog discipline, contributor onboarding (`docs/COMMUNITY.md`). |
| **Q3** | Transport hardening, persistent queues, cross-process tests. |
| **Q4** | IDL-first contracts, compatibility reports in CI, richer validation. |

## Milestones

### M1 - Runtime and Transport Hardening
- [ ] Add persistent queue mode for transient transport outages.
- [ ] Add retry/backoff strategy per topic class.
- [ ] Add transport interoperability tests across process boundaries.

### M2 - Contracts and Tooling
- [ ] Add IDL-first contract generation workflow.
- [ ] Add contract compatibility report in CI artifacts.
- [ ] Add richer type validation (nested arrays/maps).

### M3 - Operations and Reliability
- [ ] Add circuit breaker strategy for unstable nodes.
- [ ] Add checkpoint snapshots with version stamps.
- [ ] Add chaos scenarios for node crash/restart loops.

### M4 - Developer Experience
- [x] Publish getting-started tutorial with one robot pack (`docs/GETTING_STARTED.md`, `make demo`).
- [x] Add benchmark trend reporting in CI (baseline + artifacts).
- [x] Add package release workflow and changelog automation (`docs/RELEASE_PROCESS.md`, `.github/workflows/release.yml`).

## Good first issues

Issues tagged **`good first issue`** and **`help wanted`** are curated for newcomers. Ideas if none are open:

1. Add one new built-in node plugin and tests.
2. Add a new chaos scenario to `robot_core/chaos.py`.
3. Add one benchmark case to `benchmarks/benchmark_runtime.py`.
4. Improve docs with a transport comparison table.
