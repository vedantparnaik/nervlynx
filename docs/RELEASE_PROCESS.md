# Release process

## Versioning

1. Update `pyproject.toml` `version` to the next **SemVer** value (`MAJOR.MINOR.PATCH`).
2. Update `CHANGELOG.md`: move items from `[Unreleased]` into a dated section for the new version, and set the compare links at the bottom.
3. Merge to `main` via pull request when changes are ready.

## Tagging and GitHub Release

1. Ensure CI is green on `main`.
2. Create an annotated tag from the release commit:

   ```bash
   git tag -a v0.2.0 -m "Release v0.2.0"
   git push origin v0.2.0
   ```

3. The [release workflow](../.github/workflows/release.yml) builds `dist/` with `python -m build` and attaches wheels and sdists to a **GitHub Release** with auto-generated notes.

## PyPI (optional)

Publishing to PyPI is not automated in this repository by default. To publish manually after a successful build:

```bash
python -m pip install -U build twine
python -m build
twine upload dist/*
```

Use [trusted publishing](https://docs.pypi.org/trusted-publishers/) or API tokens from your PyPI account; do not commit secrets.

## Signing (optional)

For GPG-signed tags locally, maintainers may use `git tag -s`. Artifact signing (Sigstore, etc.) can be added later without changing the tag-based release flow.
