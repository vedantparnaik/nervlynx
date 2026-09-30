# __PROJECT_NAME__

A two-wheel robot that drives forward and turns away from obstacles, using one HC-SR04
ultrasonic sensor and an L298N motor driver. The same project runs in the simulator on
your laptop and on a Raspberry Pi.

## Try it in the simulator

```bash
nervlynx sim
```

Open http://127.0.0.1:9120/ to watch the robot, its distance readings, and any
collisions. Stop with Ctrl-C; a report is saved under `logs/live/`.

Run a fast, repeatable minute and fail if the robot hits anything:

```bash
nervlynx sim --fast --duration-s 60 --strict
```

## Change the behaviour

Edit `nodes/avoid.py`. It is a plain function: it gets the newest `range.front` reading
and returns a drive command (`linear` and `angular`, each -1..1). Settings such as
`stop_m` can be changed in `robot.yaml` under `params:` without touching the code.

## Wiring (BCM pin numbers)

| Part | Pin | Raspberry Pi |
| --- | --- | --- |
| L298N IN1 / IN2 (left motor) | direction | GPIO 5 / GPIO 6 |
| L298N ENA (jumper removed) | speed (PWM) | GPIO 12 |
| L298N IN3 / IN4 (right motor) | direction | GPIO 20 / GPIO 21 |
| L298N ENB (jumper removed) | speed (PWM) | GPIO 13 |
| L298N GND | ground | any GND pin |
| HC-SR04 TRIG | trigger | GPIO 23 |
| HC-SR04 ECHO | echo, **through a divider**: ECHO to 1 kΩ to GPIO 24, and GPIO 24 to 2 kΩ to GND | GPIO 24 |
| HC-SR04 VCC / GND | power | 5 V / GND |

Power the motors from their own battery through the L298N, not from the Pi, and connect
the grounds together. If a wheel spins backwards, add `invert: true` to that motor in
`robot.yaml`.

## On the Raspberry Pi

```bash
nervlynx doctor          # checks GPIO, permissions, power
nervlynx validate        # checks robot.yaml for both sim and robot
nervlynx run             # dashboard at http://<pi-name>.local:9120/
```

Lift the wheels off the ground for the first run. The motors stop on their own if the
distance sensor stops reporting, if commands stop, or when you press E-STOP.
