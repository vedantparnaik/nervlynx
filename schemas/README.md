# Schema and contract sources

| File | Role |
| --- | --- |
| `nervlynx_contracts.yaml` | Topic contract definitions (required fields, schema names, versions). |
| `shuttle.capnp` | Cap'n Proto messages for the shuttle reference stack. |

Regenerate Python/C++ contract helpers after editing YAML:

```bash
robot-core contracts-codegen
```

Validation in CI: `robot-core contracts-check`. See `robot_core/contracts.py` and `docs/ARCHITECTURE.md`.
