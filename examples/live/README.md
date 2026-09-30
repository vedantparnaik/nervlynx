# Live graph packs

Graphs for `robot-core run-live` (continuous execution). Full reference: `docs/LIVE_RUNTIME.md`.

| Pack | Hardware | Try it |
| --- | --- | --- |
| `rover_sim.yaml` | None (mock motors + kinematic plant) | `robot-core run-live examples/live/rover_sim.yaml --duration-s 30` |
| `surveillance_live.yaml` | None (existing one-shot plugins, sampled at 10 Hz) | `robot-core run-live examples/live/surveillance_live.yaml --duration-s 10` |
| `rover_bts7960.yaml` | Raspberry Pi + 4x BTS7960 (IBT-2), RPi.GPIO | `robot-core run-live examples/live/rover_bts7960.yaml --host 0.0.0.0 --allow-control` |
| `rover_tb6612.yaml` | Raspberry Pi + 2x TB6612FNG, gpiozero | `robot-core run-live examples/live/rover_tb6612.yaml --host 0.0.0.0 --allow-control` |

Validate everything without touching hardware: `robot-core live-validate examples/live/*.yaml`.
Dry-run a hardware pack on any machine with `--backend mock`.
