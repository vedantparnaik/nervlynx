# Contributing to NervLynx

Thanks for contributing.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
```

## Local checks

```bash
pytest -q
```

## Contribution guidelines

- Keep changes small and focused.
- Add or update tests for behavior changes.
- Preserve backward compatibility for message schemas when possible.
- Document new topics, node contracts, and assumptions in `README.md`.

## Pull requests

- Use clear titles and describe motivation.
- Include a brief test plan and results.
- Link issues when applicable.
