# __PROJECT_NAME__

A two-wheel robot you drive from a browser, using an L298N motor driver. The same project
runs in the simulator on your laptop and on a Raspberry Pi.

## Try it in the simulator

```bash
nervlynx sim
```

Open http://127.0.0.1:9120/ and hold W/A/S/D. The motors stop as soon as you let go
(the deadman), and E-STOP always works.

## Wiring (BCM pin numbers)

| Part | Pin | Raspberry Pi |
| --- | --- | --- |
| L298N IN1 / IN2 (left motor) | direction | GPIO 5 / GPIO 6 |
| L298N ENA (jumper removed) | speed (PWM) | GPIO 12 |
| L298N IN3 / IN4 (right motor) | direction | GPIO 20 / GPIO 21 |
| L298N ENB (jumper removed) | speed (PWM) | GPIO 13 |
| L298N GND | ground | any GND pin |

Power the motors from their own battery through the L298N, not from the Pi, and connect
the grounds together. If a wheel spins backwards, add `invert: true` to that motor in
`robot.yaml`.

## On the Raspberry Pi

```bash
nervlynx doctor          # checks GPIO, permissions, power
nervlynx run --control   # dashboard with driving at http://<pi-name>.local:9120/
```

Lift the wheels off the ground for the first run.

## Add your own code

Put a Python file in a `nodes/` folder next to `robot.yaml`:

```python
from nervlynx import node


@node(inputs=["odom"], outputs="speed.kph")
def speed_kph(odom):
  return abs(odom["speed_mps"]) * 3.6
```

then add `- plugin: speed_kph` under `nodes:` in `robot.yaml`.
