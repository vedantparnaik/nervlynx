# Threat model (baseline)

This document is a **high-level** threat model for NervLynx as a library and reference runtime. It is not a substitute for a full product security assessment for a deployed robot system.

## Trust boundaries

| Boundary | Trust assumption |
| --- | --- |
| Process boundary | In-process `PipelineRuntime` assumes trusted code; plugins run with host process privileges. |
| Transport | `InMemoryTransport` is same-process only. `ZmqJsonTransport` assumes a protected network unless you add TLS and authentication. |
| Config and graphs | YAML graph configs are **trusted input** from operators; hostile config can wire unexpected topics or plugins. |
| Traces and logs | JSONL traces may contain operational data; treat storage and replay paths as sensitive. |

## Assets

- **Runtime integrity**: correct ordering, backpressure behavior, and trace continuity.
- **Topic contracts**: schema expectations and validation at boundaries.
- **Secrets**: signing keys for `sign_payload` / distributed demo must not be committed to source control for real deployments.

## Threats (non-exhaustive)

| Threat | Mitigation direction |
| --- | --- |
| Malicious or buggy plugin code | Code review, sandboxing at deployment layer, least privilege for the host process. |
| Unauthorized publish/subscribe | `TopicAccessPolicy` and payload signing patterns in `robot_core/security.py`. |
| Dependency compromise | SBOM in CI, pin versions in production, review `CHANGELOG.md` on upgrades. |
| Replay of stale commands | Application-level sequence checks and mission state; NervLynx provides tracing hooks, not mission semantics. |

## Out of scope for this repo

- Physical robot safety certification and SIL ratings.
- Fleet-wide identity management, PKI lifecycle, and OTA update security (integrate with your platform).

## Maintenance

Review this document when adding new transports, remote control surfaces, or persistence layers.
