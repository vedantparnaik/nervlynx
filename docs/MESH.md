# Device mesh: one robot, several computers

A Raspberry Pi is great at motors, sensors, and safety, and poor at running a neural
network at 30 frames a second. An Orin NX is the other way round. The mesh lets one
`robot.yaml` run across both (or a Zero 2 W and a laptop, or three Pis): each node is
placed on one computer, and the topics that cross between computers are forwarded for
you.

```yaml
name: rover
devices:                               # the computers this robot is made of
  rover: {host: pi@rover.local}        # the first one runs every node without placement
  orin:  {host: nvidia@orin.local}
mesh:
  transport: udp                       # or zenoh
nodes:
  - {name: drive, plugin: skid_steer_drive, params: ...}
  - {name: camera, plugin: camera, params: {name: front}}
  - {name: detector, plugin: detector, placement: orin, params: {camera: front}}
  - {name: follow, plugin: follow, placement: orin}
```

Each computer runs the same project with its own device name:

```bash
nervlynx run --device rover            # on the Pi
nervlynx run --device orin             # on the Orin
```

`--device` can be left out when the computer's hostname matches a device name (or its
`devices.<name>.hostname`). To install both as services from your laptop:

```bash
nervlynx deploy --device rover --service     # uses devices.rover.host
nervlynx deploy --device orin --service
```

## What crosses the mesh

Nothing you have to list by hand. Each device sends the topics that nodes on other
devices consume (their `input_topics`), and receives the topics its own nodes consume.
In the example the detector's detections and the follow node's `cmd.drive` go from the
Orin to the Pi. Add topics to `mesh.share` to send them anyway (to watch them on another
device), and name cameras in `mesh.frames` to send their frames (Zenoh only). A shared
camera shows up as `frames("front")` on the other device, so a detector there reads it
exactly as it would a local camera, and its dashboard streams it.

`/stats` and the dashboard show the `mesh` node on every device: its peers, whether each
is online, what it sends and receives, and dropped messages.

## Safety across devices

- **The e-stop is robot-wide.** Engaging it on any device engages it on all of them, and
  clearing it on one clears it everywhere. A device that cannot clear (a critical node of
  its own is still stale) keeps reporting that it is latched, and the others latch again
  after 1.5 seconds with a reason like `orin is still e-stopped: watchdog: node stuck stale`.
- **A lost link stops the robot the usual way.** Forwarded topics stop arriving, so the
  drive deadman stops the motors and `@node` functions pause on stale inputs. The mesh
  node also reports `mesh: lost contact with orin`.
- **Simulate first.** `nervlynx sim` ignores placement and runs every node on your laptop,
  so the whole distributed robot can be tried before it exists.

## Security

Anyone on the same network can send UDP packets. Give every device the same secret and
each message is HMAC-signed; unsigned, forged, and replayed messages are dropped, and so
are messages from a device whose clock is more than `max_skew_s` (30 s) off:

```bash
export NERVLYNX_MESH_KEY="$(openssl rand -hex 16)"   # the same value on every device
```

Put it in the service environment with `systemctl --user edit nervlynx-<project>`
(`Environment=NERVLYNX_MESH_KEY=...`), or name another variable with `mesh.key_env`.

Robots that share a network must have different `mesh.robot` names (it defaults to the
config's `name`); messages meant for another robot are ignored. `nervlynx fleet deploy`
sets this for you.

## Transports

| | `udp` (default) | `zenoh` |
| --- | --- | --- |
| Install | nothing: standard library | `pip install "nervlynx[mesh]"` (prebuilt for aarch64 and armv7) |
| Finds peers | multicast on the local network, or `peers:` | multicast scouting, or `connect:` / `listen:` endpoints |
| Crosses routers / VPNs | with `peers:` addresses | yes, through Zenoh routers |
| Camera frames | no (too big for a datagram) | yes (`mesh.frames`) |

```yaml
mesh:
  transport: udp
  port: 7447                     # default
  group: 239.255.77.1            # multicast group (default)
  peers: [192.168.1.20, orin.local:7447]   # unicast instead, where multicast is blocked
  interface: 192.168.1.10        # which network interface to use for multicast
  share: [odom]                  # extra topics to send
  robot: rover-1                 # identity on a shared network
  max_skew_s: 30
```

```yaml
mesh:
  transport: zenoh
  frames: [front]                # cameras to share
  frame_fps: 10                  # at most this many frames per second each
  connect: [tcp/192.168.1.20:7447]   # optional; without it peers scout by multicast
  listen: [tcp/0.0.0.0:7447]
```

## Trying it on one computer

Two terminals, two devices:

```yaml
mesh: {transport: udp, interface: 127.0.0.1}   # loopback multicast
```

```bash
nervlynx sim --device rover --port 9120
nervlynx sim --device orin --port 9121
```

## Troubleshooting

- **Peers never appear.** Some Wi-Fi access points drop multicast (client isolation), and
  macOS asks before an app may use the local network. Use `peers:` with the other devices'
  addresses, or `transport: zenoh` with `connect:`. A failing send is reported as a fault
  on the mesh node.
- **`dropped` keeps rising.** The devices have different `NERVLYNX_MESH_KEY` values, or
  their clocks disagree (`timedatectl` on each; a Pi without network time starts with the
  wrong date).
- **"another process is also running as device rover".** Two computers (or two
  processes) were started with the same `--device`.

## Wire format

For devices that are not running NervLynx (an ESP32 can join over UDP), each message is
one UDP datagram (or one Zenoh sample under `nervlynx/<robot>/<kind>/<topic>`) holding a
JSON object:

```json
{"v": 1, "r": "rover", "d": "orin", "s": "3f9a0c1e", "n": 42, "t": 1727712345.123,
 "k": "msg", "topic": "cmd.drive", "schema": "DriveCommand", "payload": {"linear": 0.3, "angular": 0.0}}
```

`r` robot, `d` device, `s` a random id per start, `n` a sequence number that increases,
`t` Unix time. `k` is `msg`, `estop` (`engaged`, `reason`), `hb` (a heartbeat every
0.5 s: `estop`, `reason`, `nodes`), or `frame` (the JSON header, a newline, then the
image bytes). With a key the datagram is `s:<64 hex HMAC-SHA256 of the rest>:` followed
by the message.
