# Shuttle reference stack

Fixed-route robotics reference application built on the same patterns as `robot_core`.

- **CLI**: `shuttle-stack` (see `shuttle/cli.py`)
- **Modules**: broker, bus, route planning, safety hooks, sensor adapters
- **Config**: `configs/route_loop.yaml`, `configs/services.yaml`

Use shuttle as a second example of graph-style wiring and multi-process messaging alongside `robot_core` demos and `examples/robot_packs/`.
