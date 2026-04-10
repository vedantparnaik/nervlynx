# NervLynx v0.2 Roadmap

## Goals

- Mature distributed runtime mode and multi-node deployment ergonomics.
- Strengthen schema governance across Python and C++ implementations.
- Improve observability, resilience, and security posture for real deployments.

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
- [ ] Publish getting-started tutorial with one robot pack.
- [ ] Add benchmark trend reporting in CI.
- [ ] Add package release workflow and changelog automation.

## Good First Issues

1. Add one new built-in node plugin and tests.
2. Add a new chaos scenario to `robot_core/chaos.py`.
3. Add one benchmark case to `benchmarks/benchmark_runtime.py`.
4. Improve docs with a transport comparison table.
