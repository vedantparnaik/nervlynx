#!/usr/bin/env bash
# Checks the systemd watchdog against real systemd, using the user service that
# `nervlynx deploy --service` installs. CI runs it on Ubuntu; it needs a systemd user
# manager (loginctl enable-linger) and nervlynx installed for the Python on PATH.
#
#   1. A robot that starts slower than the watchdog timeout, then runs, is left alone.
#   2. A frozen process (SIGSTOP) is killed and restarted.
#   3. A hung control loop is killed with every thread's stack in the journal, and restarted.
#   4. A normal stop is clean: the run report is written and nothing restarts.
set -euo pipefail

name=ci-watchdog
project="$HOME/nervlynx-projects/$name"
unit="nervlynx-$name"

prop() { systemctl --user show "$unit" -p "$1" --value; }
journal() { journalctl --user -u "$unit" --no-pager -o cat; }
# grep -c reads everything, so journalctl never dies of SIGPIPE under pipefail.
logged() { journal | grep -c -i -- "$1" > /dev/null; }
fail() {
  echo "FAIL: $*"
  systemctl --user status "$unit" --no-pager || true
  journal | tail -n 80 || true
  exit 1
}
wait_for_restarts() {
  for _ in $(seq 60); do
    if [ "$(prop NRestarts)" -ge "$1" ] && [ "$(prop ActiveState)" = active ]; then
      return 0
    fi
    sleep 1
  done
  fail "expected $1 restart(s), saw $(prop NRestarts)"
}

rm -rf "$project"
mkdir -p "$project/nodes" "$HOME/.config/systemd/user"
cat > "$project/robot.yaml" <<'EOF'
name: ci-watchdog
runtime: {systemd_watchdog_s: 3}
nodes:
  - {plugin: slow_start}
  - {plugin: scripted_drive, rate_hz: 20, params: {steps: [{linear: 0.3, duration_s: 1}]}}
  - name: drive
    plugin: skid_steer_drive
    rate_hz: 50
    params: {driver: l298n, left: [{name: l, in1: 5, in2: 6}], right: [{name: r, in1: 13, in2: 19}]}
  - {plugin: heartbeat, params: {pin: 26}}
EOF
cat > "$project/nodes/slow_start.py" <<'EOF'
import os
import time

from nervlynx import LiveNode, node


@node(rate_hz=10)
class SlowStart(LiveNode):
  def setup(self, ctx):
    time.sleep(6)  # longer than the watchdog timeout, so arming has to wait for it

  def tick(self, ctx):
    if os.path.exists("hang"):
      os.remove("hang")
      time.sleep(3600)
EOF

python - "$(command -v nervlynx)" > "$HOME/.config/systemd/user/$unit.service" <<'EOF'
import sys
from pathlib import Path

from robot_core.remote import service_unit, target_for

target = target_for("ci@localhost", Path.home() / "nervlynx-projects" / "ci-watchdog", nervlynx=sys.argv[1], run_args="--no-server")
print(service_unit(target), end="")
EOF
cat "$HOME/.config/systemd/user/$unit.service"
systemctl --user daemon-reload
systemctl --user start "$unit"

echo "1. slow start, then healthy"
sleep 20
[ "$(prop ActiveState)" = active ] || fail "the service is not running"
[ "$(prop NRestarts)" = 0 ] || fail "a healthy robot was restarted"
logged "systemd_watchdog=3s" || fail "nervlynx did not see the notify socket"

echo "2. frozen process"
pid="$(prop MainPID)"
kill -STOP "$pid"
wait_for_restarts 1
[ "$(prop MainPID)" != "$pid" ] || fail "the frozen process is still the main process"
logged "watchdog timeout" || fail "systemd did not report a watchdog timeout"

echo "3. hung control loop"
sleep 15
touch "$project/hang"
wait_for_restarts 2
logged "line [0-9]* in tick" || fail "no thread stacks in the journal"

echo "4. normal stop"
sleep 15
systemctl --user stop "$unit"
[ "$(prop Result)" = success ] || fail "the stop was not clean: $(prop Result)"
logged "run_live_done" || fail "the run did not finish normally"
ls "$project"/logs/live/ci-watchdog-*/report.json > /dev/null || fail "no run report after a normal stop"
sleep 5
[ "$(prop ActiveState)" = inactive ] || fail "the service started again after a normal stop"

echo "systemd watchdog: all checks passed"
