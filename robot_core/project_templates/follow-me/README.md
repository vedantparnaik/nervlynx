# __PROJECT_NAME__

A two-wheel robot that follows the nearest person it sees: it turns to keep them in the
middle of the camera's view and drives closer until they fill enough of it. It uses a
Pi camera (or USB webcam), a person detector, and an L298N motor driver. The same
project runs in the simulator on your laptop and on a Raspberry Pi.

## Try it in the simulator

```bash
nervlynx sim
```

Open http://127.0.0.1:9120/: a person walks laps around the room and the robot follows.
The purple lines are the camera's field of view. Run a fast, repeatable two minutes and
fail if the robot bumps into anything (or anyone):

```bash
nervlynx sim --fast --duration-s 120 --strict
```

## Change the behaviour

Edit `nodes/follow.py`. It gets the newest `detections.front` message (boxes are
fractions of the image, so `center[0]` is 0 at the left edge and 1 at the right) and
returns a drive command. `target_height` is how much of the picture's height the person
should fill when the robot stops: bigger means closer.

## Wiring (BCM pin numbers)

| Part | Pin | Raspberry Pi |
| --- | --- | --- |
| L298N IN1 / IN2 (left motor) | direction | GPIO 5 / GPIO 6 |
| L298N ENA (jumper removed) | speed (PWM) | GPIO 12 |
| L298N IN3 / IN4 (right motor) | direction | GPIO 20 / GPIO 21 |
| L298N ENB (jumper removed) | speed (PWM) | GPIO 13 |
| L298N GND | ground | any GND pin |
| Camera | ribbon cable | CAM/DISP port (or a USB webcam) |

Power the motors from their own battery through the L298N, not from the Pi, and connect
the grounds together. Mount the camera facing forward at the front of the robot.

## On the Raspberry Pi

```bash
~/.venv/bin/pip install "nervlynx[ai] @ git+https://github.com/vedantparnaik/nervlynx"
nervlynx models get yolox-nano   # download the person detector once (3.7 MB)
nervlynx doctor                  # checks GPIO, camera, power
nervlynx run                     # dashboard at http://<pi-name>.local:9120/
```

Lift the wheels off the ground for the first run, and use the dashboard's Calibrate
section if a wheel turns the wrong way. The robot stays put when it sees nobody, and the
motors stop if the detector stops reporting, if commands stop, or when you press E-STOP.

A Pi 5 runs the detector at several frames a second, which is enough for walking pace.
For faster following, run the detector on an Orin NX or a laptop: add a `devices:`
section, place the detector there, and share the camera (see docs/MESH.md in the
NervLynx repository).
