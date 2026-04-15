# Plugin Authoring

Use `nervlynx init` to scaffold a plugin-pack project in seconds.

## Quick Start

```bash
nervlynx init my_robot_pack
cd my_robot_pack
pip install -e ".[dev]"
pytest -q
```

## Generated Structure

- `pyproject.toml`: registers sensor and node entry points
- `<package>/plugins.py`: starter camera sensor, planner stub, actuator mock plugins
- `config/graph.yaml`: starter graph wiring
- `tests/test_plugins.py`: baseline tests for plugin behavior

## Next Steps

1. Rename starter plugins to match your domain.
2. Add contract-aware payload fields for your robot stack.
3. Register additional node plugins under `nervlynx.nodes`.
4. Validate with `robot-core plugin-catalog` and `robot-core run-graph`.

## Built-in Reference Packs

Use these example graph packs to start faster:

- `examples/robot_packs/reference_camera_ingest.yaml`
- `examples/robot_packs/reference_planner_stub.yaml`
- `examples/robot_packs/reference_actuator_mock.yaml`
