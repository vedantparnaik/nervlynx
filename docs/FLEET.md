# Fleets: many robots, one project

`nervlynx deploy` puts a project on one robot. Once there are several robots running the
same project, `nervlynx fleet` updates them together, checks each one, and puts any robot
whose new release misbehaves back the way it was.

## fleet.yaml

Beside `robot.yaml`, list the robots:

```yaml
defaults:                                  # apply to every robot unless it says otherwise
  remote_nervlynx: ~/.venv/bin/nervlynx
  run_args: --control
robots:
  rover-1: {host: pi@rover-1.local}
  rover-2: {host: pi@rover-2.local, overlay: fleet/rover-2.yaml}
  rover-3: {host: pi@rover-3.tailnet.ts.net, port: 9121}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `host` | required | `user@host` or an ssh config name |
| `overlay` | none | A file of settings for this robot only (see below) |
| `run_args` | none | Extra `nervlynx run` options for its service |
| `remote_nervlynx` | `nervlynx` | The nervlynx command on the robot |
| `port` | 9120 | Its dashboard port, used for health checks |
| `pip` | next to `remote_nervlynx` | The pip used by `fleet upgrade` |

The robots need SSH keys set up (`ssh-copy-id pi@rover-1.local`), because fleet runs
without asking for passwords.

## Per-robot settings

Robots are rarely identical: one has its motors on different pins, another is heavier
and needs a lower speed limit. Keep `robot.yaml` shared and put the differences in an
overlay, in the same format as `calibration.yaml`:

```yaml
# fleet/rover-2.yaml
nodes:
  drive:
    params:
      max_speed: 0.4
      left: [{name: left, in1: 17, in2: 27}]
```

`fleet deploy` writes each robot's overlay to `overlay.yaml` on that robot. If the project
uses the mesh, it also gives each robot its own `mesh.robot` name, so robots sharing a
network never act on each other's messages. Each robot's `calibration.yaml` (from the
dashboard wizard) and its recorded runs stay on the robot and are never overwritten.

## Deploy

```bash
nervlynx fleet deploy                     # every robot, four at a time
nervlynx fleet deploy --robots rover-2    # just one
```

For each robot:

1. The release it is running is kept on the robot (`~/nervlynx-projects/.releases/`, the
   last five).
2. The project is copied, with that robot's overlay and a release stamp.
3. `nervlynx validate` runs on the robot. If it fails, the old release comes back and
   nothing is restarted.
4. The service is installed or updated and restarted.
5. Fleet waits (up to `--health-timeout-s`, 20 s) for the dashboard's `/health` to report
   the robot healthy and stay that way for 3 seconds. A robot that is latched in e-stop
   counts as healthy only if no node is failing (so `start_in_estop` is fine, a dead
   sensor is not).
6. If it doesn't become healthy, the robot goes back to its previous release and restarts
   (`--no-rollback` leaves it on the new one to debug).

The summary shows what happened on each robot, and the command exits with 1 if any robot
failed or was rolled back:

```text
robot    result                          health  release
rover-1  deployed                        ok      20260930-221503
rover-2  unhealthy (fault); rolled back  ok      20260929-101500
```

## Status, rollback, and upgrades

```bash
nervlynx fleet status                          # service, health, release, NervLynx version
nervlynx fleet rollback                        # back to the release before the current one
nervlynx fleet rollback --to 20260929-101500   # or to a particular one
nervlynx fleet upgrade --ref v0.3.0            # a new NervLynx on every robot, over the air
nervlynx fleet upgrade --extras ai,mesh        # with extras
nervlynx fleet list                            # what fleet.yaml says, without the network
```

`upgrade` installs the new version with the robot's pip, restarts, and checks health like
a deploy does.

## Remote access

Fleet only needs SSH. For robots that are not on your network (at a customer's site, on
mobile data), the simplest route is [Tailscale](https://tailscale.com): install it on the
laptop and each robot (`curl -fsSL https://tailscale.com/install.sh | sh && sudo
tailscale up`), then use the robots' Tailscale names as their `host`. Their dashboards
are then reachable at `http://<robot>:9120/` from anywhere on your tailnet, and `sudo
tailscale serve --bg 9120` gives a dashboard an https address, which also lets the
browser's voice input work.

Keep dashboard control behind a token on shared networks: add `--control-token` (or
`NERVLYNX_CONTROL_TOKEN` in the service environment) to `run_args`.
