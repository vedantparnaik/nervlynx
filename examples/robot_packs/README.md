# Example robot packs

YAML graph configs you can run with `robot-core run-graph <file> --output logs/trace.jsonl`.

| Pack | Purpose |
| --- | --- |
| `surveillance.yaml` | Default surveillance-style seed and perception/planner wiring. |
| `delivery.yaml` | Delivery profile seed with perception and planner nodes. |
| `warehouse.yaml` | Indoor-style seed with weaker GPS assumptions. |
| `reference_camera_ingest.yaml` | Reference camera ingest path through perception, planner stub, actuator mock. |
| `reference_planner_stub.yaml` | Planner stub and actuator mock from a perception seed. |
| `reference_actuator_mock.yaml` | Minimal actuator mock from a mission command seed. |

Quick local run (requires venv / `pip install -e ".[dev]"`):

```bash
robot-core run-graph examples/robot_packs/surveillance.yaml --output logs/example_graph.jsonl
```

Or use **`make graph-example`** from the repository root after `make setup`.
