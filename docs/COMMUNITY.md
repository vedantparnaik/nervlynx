# Community and contributing

## Issue labels

Maintainers use these labels to route work. When opening an issue, pick the closest fit; we may relabel.

Canonical definitions live in [`.github/labels.yml`](../.github/labels.yml). Pushing changes to that file on `main` runs the **sync-labels** workflow so the repository labels stay aligned with this table.

| Label | Meaning |
| --- | --- |
| `good first issue` | Small, bounded tasks ideal for first-time contributors. |
| `help wanted` | Maintainer-approved; community contributions welcome. |
| `bug` | Incorrect or unexpected behavior. |
| `enhancement` | New feature or meaningful improvement. |
| `documentation` | README, guides, or API docs only. |
| `runtime` | `robot_core` execution, scheduling, transport. |
| `ci` | Workflows, baselines, or automation. |

## Good first issues

Look for issues tagged **`good first issue`**. Typical starter work:

- Add or extend tests for an existing module.
- Improve docstrings or user-facing docs (`docs/`, `README.md`).
- Add a small benchmark or smoke scenario following existing patterns.

## Quarterly themes (2026)

Themes help focus reviews and releases; they are not hard gates.

| Quarter | Theme |
| --- | --- |
| Q2 | Release discipline, plugin ergonomics, CI confidence. |
| Q3 | Transport hardening and distributed runtime polish. |
| Q4 | Contract governance and cross-language (Python/C++) parity. |

See `ROADMAP.md` for detailed milestones.

## Discussions and questions

Use [GitHub Discussions](https://github.com/vedantparnaik/nervlynx/discussions) for design questions and support-style threads; use Issues for actionable bugs and features.
