# Deploy layouts

This directory holds **reference** deployment material. Validate paths, users, and secrets for your environment before production use.

| Path | Role |
| --- | --- |
| `config/` | Example graph YAML (`graph_surveillance.yaml`) and robot profile (`robot_profile.yaml`). |
| `docker/` | Dockerfile and compose snippet for containerized runs. |
| `systemd/` | Example unit file for edge-style installs. |
| `scripts/` | Edge install (`install_edge.sh`) and config sync (`sync_config.py`). |

See also: `README.md` (Deployment Shortcuts) and `docs/RELEASE_PROCESS.md` for release artifacts.
