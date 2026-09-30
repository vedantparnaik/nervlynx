# ESP32 link (NervLynx Link v1)

Linux on a Raspberry Pi is not real-time: a busy CPU can delay a motor update or an
encoder read by tens of milliseconds. A microcontroller next to the Pi has no such
problem. With the link, the ESP32 drives the motors, counts encoder pulses, and stops the
motors on its own if the Pi stops sending commands, whether the Pi crashed, the process
hung, or the USB cable came out. The Pi keeps the behaviour, safety layers, and dashboard.

Status: the host side (`esp32_link` node, `robot_core/link.py`) is tested against a
simulated board. The Arduino firmware in `firmware/esp32_link/` follows the same protocol
but **has not been tested on hardware yet**.

## Protocol

Newline-delimited JSON over USB serial at 115200 baud, so you can watch it in any serial
monitor:

| Direction | Message | When |
| --- | --- | --- |
| Pi to board | `{"t":"hello"}` | on connect |
| Pi to board | `{"t":"drive","l":-1..1,"r":-1..1}` | every tick (50 Hz); zeros when idle, stale, or e-stopped |
| board to Pi | `{"t":"hello","fw":"nervlynx-link","v":1,"board":"esp32"}` | reply to hello |
| board to Pi | `{"t":"enc","l":ticks,"r":ticks}` | 50 Hz, cumulative counts |
| board to Pi | `{"t":"wd"}` | the board stopped the motors: no drive command for 300 ms |
| board to Pi | `{"t":"err","msg":"..."}` | anything the board wants to report |

## Flash the board

1. Install the Arduino IDE, the ESP32 board package (Espressif), and the ArduinoJson
   library (version 7) from the Library Manager.
2. Open `firmware/esp32_link/esp32_link.ino`, set the pin numbers at the top to match your
   wiring, pick your board and port, and upload.
3. Open the serial monitor at 115200 baud and type `{"t":"hello"}`: the board should reply.

Default wiring (L298N with both EN jumpers removed):

| ESP32 | L298N / encoder |
| --- | --- |
| GPIO 25, 26, 27 | ENA, IN1, IN2 (left motor) |
| GPIO 14, 32, 33 | ENB, IN3, IN4 (right motor) |
| GPIO 34, 35 | left and right encoder signal |
| GND | L298N GND and encoder GND |

## Use it from a robot

Replace the `skid_steer_drive` node with the link (same `cmd.drive` commands):

```yaml
nodes:
  - plugin: esp32_link
    params:
      port: auto          # or /dev/ttyUSB0; `nervlynx scan` lists candidates
      max_speed: 0.6
      deadman_s: 0.25
```

It publishes `link.encoders` (`{"left_ticks", "right_ticks"}`) and shows the port,
firmware, connection state, and watchdog trips in the dashboard. With `backend: mock` (or
`auto` on a laptop) it talks to a simulated board, so the config also runs in `nervlynx
sim`. Add `wheel_odometry` to turn the counts into `odom` (position and speed) for skills
and the ROS 2 bridge:

```yaml
  - plugin: wheel_odometry
    params: {ticks_per_meter: 4700, track_width_m: 0.16}
```

Next steps: closed-loop wheel speed (PID on the board), servo channels, IMU reads on the
board, and a MicroPython build for the Raspberry Pi Pico.
