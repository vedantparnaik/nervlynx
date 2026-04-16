# Security policy

## Supported versions

Security fixes are applied to the **latest minor release** on `main` first, then backported to the previous minor release when practical. Use the newest tagged release for production evaluations.

| Version | Supported |
| --- | --- |
| 0.2.x | Yes |
| 0.1.x | Best effort (upgrade recommended) |

## Reporting a vulnerability

**Please do not** open a public GitHub issue for undisclosed security vulnerabilities.

Instead, email the maintainer with:

- A short description of the issue and its impact
- Steps to reproduce (commands, config, or proof-of-concept)
- Affected versions or commit SHA if known
- Whether you need coordinated disclosure timing

We aim to acknowledge reports within **5 business days** and to provide a remediation timeline when possible.

For low-risk hardening suggestions (e.g. dependency bumps, CI hardening), a public issue or pull request is welcome.

## Disclosure process

1. Maintainer confirms receipt and severity.
2. Fix is developed on a private branch or embargoed PR where needed.
3. Patch is released with a clear `CHANGELOG.md` entry and tag.
4. Reporter is credited in the release notes unless they prefer to remain anonymous.

## Scope

In scope: NervLynx Python package (`robot_core`, `shuttle`), C++ reference runtime (`cpp_core`), CI workflows, and documented deployment scripts under `deploy/`.

Out of scope by default: third-party robot hardware firmware, your custom graph payloads, and deployment environments not reproducible from this repository.

## Hardening baseline

See `docs/THREAT_MODEL.md` for trust boundaries and known limitations. SBOM artifacts are produced in CI (`.github/workflows/sbom.yml`) for supply-chain visibility.
