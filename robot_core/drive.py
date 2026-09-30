"""Skid-steer (differential) drive actuation for `LiveRuntime`.

`SkidSteerDrive` turns `cmd.drive` messages into motor duty at a fixed control rate and
guarantees the motors stop when commands go stale (deadman), when the e-stop latches, or
when the runtime shuts down. The shaping stages exist because small DC gearmotors do not
behave linearly:

  floor   remap any non-zero request onto [min_duty, 1] so small commands still move
  kick    a short high-duty pulse breaks static friction when a side starts from rest
  slew    ramp duty changes (stops excepted) so four motors never step to full at once
          and sag the battery
  reverse coast through zero instead of slamming the H-bridge the other way

Command payloads (normalised to -1..1): {"left": l, "right": r} or {"linear": v, "angular": w}.
"""

from __future__ import annotations

import math
import threading
from dataclasses import asdict, dataclass, fields
from typing import Any, Iterable

from robot_core.hardware import BACKENDS, DRIVERS, Motor, PinBackend, build_motor, create_backend, validate_motor_specs
from robot_core.live import LiveNode, NodeContext, Output
from robot_core.metrics import label_key
from robot_core.runtime import RuntimeMessage


@dataclass(frozen=True)
class DriveTuning:
  min_duty: float = 0.18
  turn_min_duty: float = 0.26
  kick_duty: float = 0.55
  kick_s: float = 0.18
  kick_rearm_s: float = 0.5
  slew_per_s: float = 4.0
  deadband: float = 0.01

  @classmethod
  def from_dict(cls, raw: dict[str, Any] | None) -> DriveTuning:
    raw = dict(raw or {})
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(raw) - known)
    if unknown:
      raise ValueError(f"unknown tuning fields: {', '.join(unknown)}")
    values = {key: float(value) for key, value in raw.items()}
    tuning = cls(**values)
    for name in ("min_duty", "turn_min_duty", "kick_duty", "deadband"):
      if not 0.0 <= getattr(tuning, name) < 1.0:
        raise ValueError(f"tuning.{name} must be in [0, 1)")
    for name in ("kick_s", "kick_rearm_s"):
      if getattr(tuning, name) < 0.0:
        raise ValueError(f"tuning.{name} must be >= 0")
    if tuning.slew_per_s <= 0.0:
      raise ValueError("tuning.slew_per_s must be > 0")
    return tuning


def shape(cmd: float, floor: float, deadband: float = 0.01) -> float:
  """Remap a -1..1 request onto the band the motors can act on."""
  if abs(cmd) < deadband:
    return 0.0
  magnitude = floor + min(abs(cmd), 1.0) * (1.0 - floor)
  return magnitude if cmd > 0 else -magnitude


def approach(current: float, goal: float, step: float) -> float:
  if goal > current:
    return min(current + step, goal)
  return max(current - step, goal)


def mix_arcade(linear: float, angular: float) -> tuple[float, float]:
  """Linear/angular (both -1..1) to left/right, scaled down together if either exceeds 1."""
  left, right = linear - angular, linear + angular
  peak = max(abs(left), abs(right), 1.0)
  return left / peak, right / peak


class SideShaper:
  """Kick/slew/reversal state machine for one side of a skid-steer chassis."""

  def __init__(self, tuning: DriveTuning) -> None:
    self.tuning = tuning
    self.applied = 0.0
    self.kick_until_s = 0.0
    self.zero_since_s = -math.inf
    self.kicks = 0

  @property
  def kicking(self) -> bool:
    return self.kick_until_s > 0.0 and self.applied != 0.0

  def reset(self, now_s: float) -> None:
    if self.applied != 0.0:
      self.zero_since_s = now_s
    self.applied = 0.0
    self.kick_until_s = 0.0

  def update(self, target: float, dt: float, now_s: float) -> float:
    t = self.tuning
    step = t.slew_per_s * dt
    if target == 0.0:
      self.reset(now_s)  # stops are immediate, never ramped
    elif self.applied != 0.0 and target * self.applied < 0:
      self.kick_until_s = 0.0
      self.applied = approach(self.applied, 0.0, step)
      if abs(self.applied) < 0.01:
        self.applied = 0.0
        self.zero_since_s = now_s
    elif self.applied == 0.0:
      if t.kick_s > 0.0 and now_s - self.zero_since_s >= t.kick_rearm_s:
        # Wheels have been at rest long enough that stiction needs breaking.
        self.kick_until_s = now_s + t.kick_s
        self.applied = t.kick_duty if target > 0 else -t.kick_duty
        self.kicks += 1
      else:
        # Only just stopped, so the wheels are still turning: ramp in normally.
        self.applied = approach(0.0, target, step)
    elif now_s < self.kick_until_s:
      self.applied = t.kick_duty if target > 0 else -t.kick_duty
    else:
      self.kick_until_s = 0.0
      self.applied = approach(self.applied, target, step)
    return self.applied


_TEST_MOVES = {"forward": (1.0, 1.0), "backward": (-1.0, -1.0), "left": (-1.0, 1.0), "right": (1.0, -1.0)}
_MAX_TEST_S = 3.0
_CALIBRATED_TUNING = ("min_duty", "turn_min_duty")


class SkidSteerDrive(LiveNode):
  """Actuator node for a skid-steer rover (any number of motors per side).

  `swap_sides: true` means the motors listed under `left` are on the robot's right (the
  calibration wizard sets it when "turn left" turns right).
  """

  def __init__(
    self,
    *,
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    driver: str = "bts7960",
    backend: str = "mock",
    pwm_frequency_hz: float = 1000.0,
    command_topic: str = "cmd.drive",
    state_topic: str = "drive.state",
    deadman_s: float = 0.25,
    max_speed: float = 1.0,
    state_every_n_ticks: int = 2,
    tuning: dict[str, Any] | None = None,
    swap_sides: bool = False,
  ) -> None:
    if backend not in BACKENDS:
      raise ValueError(f"unknown backend {backend!r}; expected one of {', '.join(BACKENDS)}")
    if driver not in DRIVERS:
      raise ValueError(f"unknown driver {driver!r}; expected one of {', '.join(DRIVERS)}")
    if not isinstance(left, list) or not left or not isinstance(right, list) or not right:
      raise ValueError("left and right must each be a non-empty list of motor specs")
    issues = validate_motor_specs(driver, list(left) + list(right))
    if issues:
      raise ValueError("; ".join(issues))
    if deadman_s <= 0:
      raise ValueError("deadman_s must be > 0")
    if not 0.0 < max_speed <= 1.0:
      raise ValueError("max_speed must be in (0, 1]")
    if state_every_n_ticks < 1:
      raise ValueError("state_every_n_ticks must be >= 1")
    if not isinstance(swap_sides, bool):
      raise ValueError("swap_sides must be true or false")
    self.swap_sides = swap_sides
    self.input_topics = (command_topic,)
    self.left_specs = [dict(spec) for spec in left]
    self.right_specs = [dict(spec) for spec in right]
    self.driver = driver
    self.backend_name = backend
    self.pwm_frequency_hz = float(pwm_frequency_hz)
    self.command_topic = command_topic
    self.state_topic = state_topic
    self.deadman_ns = int(deadman_s * 1e9)
    self.max_speed = float(max_speed)
    self.state_every_n_ticks = int(state_every_n_ticks)
    self.tuning = DriveTuning.from_dict(tuning)
    self.backend: PinBackend | None = None
    self.left_motors: list[Motor] = []
    self.right_motors: list[Motor] = []
    self._left = SideShaper(self.tuning)
    self._right = SideShaper(self.tuning)
    self._target = (0.0, 0.0)
    self._last_cmd_ns: int | None = None
    self._cmd_trace: str | None = None
    self._cmd_pending = False
    self._last_tick_ns: int | None = None
    self._ticks = 0
    self._commands = 0
    self._rejected = 0
    self._deadman_active = True
    self._deadman_stops = 0
    self._hard_lock = threading.Lock()
    self._hard_stopped = False
    self._saw_estop = False
    self._kicks_reported = [0, 0]
    self._gauges: dict[str, Any] = {}
    self._counters: dict[str, Any] = {}
    self._test: dict[str, Any] | None = None
    self._calibrated: set[str] = set()

  # ------------------------------------------------------------------ lifecycle

  def setup(self, ctx: NodeContext) -> None:
    self.backend = create_backend(self.backend_name)
    if self.backend_name == "auto":
      ctx.fault(f"hardware backend auto resolved to {self.backend.name}", severity="info", kind="hardware")
    try:
      self.left_motors = [build_motor(self.backend, self.driver, spec, frequency_hz=self.pwm_frequency_hz) for spec in self.left_specs]
      self.right_motors = [build_motor(self.backend, self.driver, spec, frequency_hz=self.pwm_frequency_hz) for spec in self.right_specs]
    except Exception:
      self.backend.close()
      raise
    if self.swap_sides:
      self.left_motors, self.right_motors = self.right_motors, self.left_motors
    self._stop_motors()
    m = ctx.metrics
    for side in ("left", "right"):
      side_labels = label_key({"side": side})
      self._gauges[f"target_{side}"] = m.gauge("nervlynx_drive_target", side_labels)
      self._gauges[f"applied_{side}"] = m.gauge("nervlynx_drive_applied_duty", side_labels)
      self._counters[f"kicks_{side}"] = m.counter("nervlynx_drive_kicks_total", side_labels)
    for motor in self.left_motors + self.right_motors:
      self._gauges[f"motor_{motor.name}"] = m.gauge("nervlynx_motor_duty", label_key({"motor": motor.name}))
    self._gauges["command_age"] = m.gauge("nervlynx_drive_command_age_seconds")
    self._gauges["deadman"] = m.gauge("nervlynx_drive_deadman_active")
    self._counters["commands"] = m.counter("nervlynx_drive_commands_total")
    self._counters["rejected"] = m.counter("nervlynx_drive_commands_rejected_total")
    self._counters["deadman"] = m.counter("nervlynx_drive_deadman_stops_total")
    m.describe("nervlynx_drive_target", "Shaped drive target per side (-1..1).")
    m.describe("nervlynx_drive_applied_duty", "Duty applied per side after kick and slew (-1..1).")
    m.describe("nervlynx_motor_duty", "Signed H-bridge duty per motor after inversion (-1..1).")
    m.describe("nervlynx_drive_deadman_stops_total", "Times the drive stopped because commands went stale.")
    m.describe("nervlynx_drive_kicks_total", "Stiction-breaking kick pulses per side.")

  def teardown(self, ctx: NodeContext) -> None:
    self._stop_motors()
    if self.backend is not None:
      self.backend.close()

  def safe_stop(self, ctx: NodeContext) -> None:
    now_s = ctx.now_ns / 1e9
    self._test = None
    self._left.reset(now_s)
    self._right.reset(now_s)
    self._target = (0.0, 0.0)
    # Forget the last command so motion only resumes on a fresh one.
    self._last_cmd_ns = None
    self._cmd_pending = False
    self._stop_motors()
    if self._gauges:
      self._publish_metrics(0.0, 0.0, ctx.now_ns)

  def hard_stop(self) -> None:
    with self._hard_lock:
      self._hard_stopped = True
      self._test = None
    self._stop_motors()

  # ------------------------------------------------------------------ control

  def on_message(self, msg: RuntimeMessage, ctx: NodeContext) -> Iterable[Output] | None:
    if msg.envelope.topic != self.command_topic:
      return None
    parsed = self._parse(msg.payload)
    if parsed is None:
      self._rejected += 1
      if "rejected" in self._counters:
        self._counters["rejected"].inc()
      ctx.fault(f"rejected drive command payload keys={sorted(msg.payload)}", kind="bad_command")
      return None
    if ctx.estop_engaged:
      return None
    if self._test is not None:
      self._end_test(ctx.now_ns / 1e9)
      ctx.fault("calibration test stopped: a drive command arrived", severity="info", kind="calibration")
    self._target = parsed
    self._last_cmd_ns = ctx.now_ns
    self._cmd_trace = msg.envelope.trace_id
    self._cmd_pending = True
    self._commands += 1
    if "commands" in self._counters:
      self._counters["commands"].inc()
    return None

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    now_ns = ctx.now_ns
    now_s = now_ns / 1e9
    dt = 0.02 if self._last_tick_ns is None else (now_ns - self._last_tick_ns) / 1e9
    dt = max(1e-3, min(dt, 0.5))
    self._last_tick_ns = now_ns
    self._ticks += 1

    with self._hard_lock:
      if ctx.estop_engaged:
        self._saw_estop = True
      elif self._saw_estop:
        # The operator cleared the e-stop; release the hard-stop latch with it.
        self._saw_estop = False
        self._hard_stopped = False
      halted = self._hard_stopped or ctx.estop_engaged

    if self._test is not None:
      if halted or now_ns >= self._test["until_ns"]:
        self._end_test(now_s)
      else:
        return self._run_test(ctx)

    left, right = self._target
    fresh = self._last_cmd_ns is not None and now_ns - self._last_cmd_ns <= self.deadman_ns
    if halted:
      left = right = 0.0
    elif not fresh:
      if not self._deadman_active:
        self._deadman_active = True
        self._deadman_stops += 1
        self._counters["deadman"].inc()
      self._target = (0.0, 0.0)
      left = right = 0.0
    else:
      self._deadman_active = False

    floor = self.tuning.turn_min_duty if left * right < 0 else self.tuning.min_duty
    target_l = shape(left * self.max_speed, floor, self.tuning.deadband)
    target_r = shape(right * self.max_speed, floor, self.tuning.deadband)
    applied_l = self._left.update(target_l, dt, now_s)
    applied_r = self._right.update(target_r, dt, now_s)
    if halted:
      applied_l = applied_r = 0.0
    for motor in self.left_motors:
      motor.drive(applied_l)
    for motor in self.right_motors:
      motor.drive(applied_r)

    self._publish_metrics(target_l, target_r, now_ns)
    applied_command = self._cmd_pending and not halted and fresh
    if applied_command or self._ticks % self.state_every_n_ticks == 0:
      ctx.publish(
        self.state_topic,
        "DriveState",
        self._state_payload(target_l, target_r, halted),
        trace_id=self._cmd_trace if applied_command else None,
      )
      self._cmd_pending = False
    return None

  def status(self) -> dict[str, Any]:
    return {
      "driver": self.driver,
      "backend": self.backend.name if self.backend is not None else self.backend_name,
      "target": {"left": round(self._target[0], 4), "right": round(self._target[1], 4)},
      "applied": {"left": round(self._left.applied, 4), "right": round(self._right.applied, 4)},
      "motors": {m.name: round(m.duty, 4) for m in self.left_motors + self.right_motors},
      "deadman_active": self._deadman_active,
      "deadman_stops": self._deadman_stops,
      "commands": self._commands,
      "rejected_commands": self._rejected,
      "kicks": {"left": self._left.kicks, "right": self._right.kicks},
      "hard_stopped": self._hard_stopped,
      "testing": self._test["label"] if self._test is not None else None,
    }

  # ------------------------------------------------------------------ calibration

  def calibrate(self, action: str, args: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    """Wizard actions: describe, spin (one motor), test (forward/backward/left/right),
    stop, invert, swap_sides, tuning. Tests run for at most 3 s and stop on any command."""
    motors = {m.name: m for m in self.left_motors + self.right_motors}
    if action == "describe":
      pass
    elif action == "stop":
      self._end_test(ctx.now_ns / 1e9)
    elif action == "invert":
      motor = motors.get(str(args.get("motor")))
      if motor is None:
        raise ValueError(f"no motor named {args.get('motor')!r}; motors: {', '.join(motors)}")
      motor.invert = bool(args["value"]) if "value" in args else not motor.invert
      self._calibrated.add(f"invert:{motor.name}")
    elif action == "swap_sides":
      value = bool(args.get("value", not self.swap_sides))
      if value != self.swap_sides:
        self.left_motors, self.right_motors = self.right_motors, self.left_motors
        self.swap_sides = value
      self._calibrated.add("swap_sides")
    elif action == "tuning":
      changes = {key: float(args[key]) for key in _CALIBRATED_TUNING if key in args}
      if not changes:
        raise ValueError(f"tuning needs one of: {', '.join(_CALIBRATED_TUNING)}")
      self.tuning = DriveTuning.from_dict({**asdict(self.tuning), **changes})
      self._left.tuning = self._right.tuning = self.tuning
      self._calibrated.update(f"tuning:{key}" for key in changes)
    elif action in ("spin", "test"):
      self._start_test(action, args, motors, ctx)
    else:
      raise ValueError(f"unknown drive calibration action {action!r}")
    return self._describe(ctx)

  def calibration(self) -> dict[str, Any] | None:
    by_name = {m.name: m for m in self.left_motors + self.right_motors}
    patch: dict[str, Any] = {}
    for side, specs in (("left", self.left_specs), ("right", self.right_specs)):
      items = [
        {"name": spec["name"], "invert": by_name[spec["name"]].invert}
        for spec in specs
        if isinstance(spec.get("name"), str) and f"invert:{spec['name']}" in self._calibrated
      ]
      if items:
        patch[side] = items
    if "swap_sides" in self._calibrated:
      patch["swap_sides"] = self.swap_sides
    tuning = {key: getattr(self.tuning, key) for key in _CALIBRATED_TUNING if f"tuning:{key}" in self._calibrated}
    if tuning:
      patch["tuning"] = tuning
    return patch or None

  def _describe(self, ctx: NodeContext) -> dict[str, Any]:
    named = all(isinstance(spec.get("name"), str) for spec in self.left_specs + self.right_specs)
    return {
      "kind": "drive",
      "sides": {
        side: [{"name": m.name, "invert": m.invert} for m in motors]
        for side, motors in (("left", self.left_motors), ("right", self.right_motors))
      },
      "swap_sides": self.swap_sides,
      "tuning": {key: getattr(self.tuning, key) for key in _CALIBRATED_TUNING},
      "max_speed": self.max_speed,
      "testing": self._test["label"] if self._test is not None else None,
      "estop": ctx.estop_engaged,
      "savable": named,
    }

  def _start_test(self, action: str, args: dict[str, Any], motors: dict[str, Motor], ctx: NodeContext) -> None:
    if ctx.estop_engaged or self._hard_stopped:
      raise ValueError("clear the e-stop first; the wheels will turn")
    try:
      duty = float(args.get("duty", 0.4))
      seconds = float(args.get("seconds", 1.0))
    except (TypeError, ValueError):
      raise ValueError("duty and seconds must be numbers") from None
    if not 0.0 < duty <= 1.0:
      raise ValueError("duty must be in (0, 1]")
    duty = min(duty, self.max_speed)
    seconds = max(0.1, min(seconds, _MAX_TEST_S))
    if action == "spin":
      name = str(args.get("motor"))
      if name not in motors:
        raise ValueError(f"no motor named {name!r}; motors: {', '.join(motors)}")
      test: dict[str, Any] = {"motor": name, "duty": duty, "label": f"spin {name} forward at {duty:.2f}"}
    else:
      move = str(args.get("move", "forward"))
      if move not in _TEST_MOVES:
        raise ValueError(f"move must be one of {', '.join(_TEST_MOVES)}")
      left, right = _TEST_MOVES[move]
      test = {"left": left * duty, "right": right * duty, "label": f"drive {move} at {duty:.2f}"}
    self._target = (0.0, 0.0)
    self._last_cmd_ns = None
    test["until_ns"] = ctx.now_ns + int(seconds * 1e9)
    self._test = test

  def _run_test(self, ctx: NodeContext) -> None:
    test = self._test
    assert test is not None
    if "motor" in test:
      for motor in self.left_motors + self.right_motors:
        motor.drive(test["duty"] if motor.name == test["motor"] else 0.0)
      on_left = any(m.name == test["motor"] for m in self.left_motors)
      left, right = (test["duty"], 0.0) if on_left else (0.0, test["duty"])
    else:
      left, right = test["left"], test["right"]
      for motor in self.left_motors:
        motor.drive(left)
      for motor in self.right_motors:
        motor.drive(right)
    self._publish_metrics(left, right, ctx.now_ns)
    payload = self._state_payload(left, right, False)
    payload.update({"left_applied": round(left, 4), "right_applied": round(right, 4), "test": test["label"]})
    ctx.publish(self.state_topic, "DriveState", payload)
    return None

  def _end_test(self, now_s: float) -> None:
    if self._test is None:
      return
    self._test = None
    self._left.reset(now_s)
    self._right.reset(now_s)
    self._stop_motors()

  # ------------------------------------------------------------------ helpers

  def _parse(self, payload: dict[str, Any]) -> tuple[float, float] | None:
    try:
      if "left" in payload and "right" in payload:
        left, right = float(payload["left"]), float(payload["right"])
      elif "linear" in payload:
        left, right = mix_arcade(float(payload["linear"]), float(payload.get("angular", 0.0)))
      else:
        return None
    except (TypeError, ValueError):
      return None
    if not (math.isfinite(left) and math.isfinite(right)):
      return None
    return max(-1.0, min(1.0, left)), max(-1.0, min(1.0, right))

  def _stop_motors(self) -> None:
    for motor in self.left_motors + self.right_motors:
      motor.stop()

  def _publish_metrics(self, target_l: float, target_r: float, now_ns: int) -> None:
    g = self._gauges
    g["target_left"].set(target_l)
    g["target_right"].set(target_r)
    g["applied_left"].set(self._left.applied)
    g["applied_right"].set(self._right.applied)
    for motor in self.left_motors + self.right_motors:
      g[f"motor_{motor.name}"].set(motor.duty)
    g["command_age"].set((now_ns - self._last_cmd_ns) / 1e9 if self._last_cmd_ns is not None else -1.0)
    g["deadman"].set(1 if self._deadman_active else 0)
    for idx, (side, shaper) in enumerate((("left", self._left), ("right", self._right))):
      delta = shaper.kicks - self._kicks_reported[idx]
      if delta:
        self._counters[f"kicks_{side}"].inc(delta)
        self._kicks_reported[idx] = shaper.kicks

  def _state_payload(self, target_l: float, target_r: float, halted: bool) -> dict[str, Any]:
    return {
      "left_target": round(target_l, 4),
      "right_target": round(target_r, 4),
      "left_applied": round(self._left.applied, 4),
      "right_applied": round(self._right.applied, 4),
      "left_kicking": self._left.kicking,
      "right_kicking": self._right.kicking,
      "motors": {m.name: round(m.duty, 4) for m in self.left_motors + self.right_motors},
      "deadman": self._deadman_active,
      "halted": halted,
    }
