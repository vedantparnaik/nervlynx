# Deprecation Policy

NervLynx uses **Semantic Versioning** (`MAJOR.MINOR.PATCH`) for the Python package and public APIs.

## Before 1.0

- **0.x releases** may introduce breaking changes when required for correctness, security, or major design improvements.
- The **`robot_core.stable_api`** module and symbols listed in `docs/API_STABILITY.md` receive stronger compatibility guarantees within the same minor line (0.x): additive changes preferred; removals only with a deprecation period when feasible.

## Deprecation process

1. **Announce**: Deprecated APIs are marked in docstrings or release notes in `CHANGELOG.md` under a dated heading.
2. **Minimum period**: Where practical, deprecated APIs remain for at least **one minor release** (e.g. deprecated in 0.2.x, removed no earlier than 0.3.0).
3. **Migration**: Each deprecation entry should link to a replacement or migration snippet when possible.

## Experimental surfaces

- Deep imports (e.g. `robot_core.some_internal_module`) are **not** covered by the stable API policy unless explicitly documented.
- CLI subcommands and graph YAML fields may evolve; breaking changes are called out in `CHANGELOG.md`.

## After 1.0

- Breaking changes require a **MAJOR** version bump.
- Deprecations follow the same announce-then-remove pattern with a longer minimum window where community impact warrants it.
