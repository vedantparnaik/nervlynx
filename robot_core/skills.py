"""Skills: named, bounded robot actions that people, voice, and LLM agents can ask for.

A skill is a generator that runs a little on every tick of the `skills` node, which is
the only thing that executes them. Skills never touch motors: they publish `cmd.drive`
like any other node, so the drive deadman, `max_speed`, and the e-stop still apply, and
the runner adds its own gates: arguments are checked against each skill's limits, an
e-stop cancels the whole plan, and a drive command from anyone else (you, on the
dashboard) takes over immediately.

  from nervlynx import skill

  @skill(params={"times": (1, 5)})
  def wave(robot, *, times=2):
    '''Wave the arm.'''
    for _ in range(times):
      robot.send("cmd.servo", {"arm": 150})
      yield from robot.wait(0.5)
      robot.send("cmd.servo", {"arm": 30})
      yield from robot.wait(0.5)

Plans arrive on `agent.plan` as {"steps": [{"skill": "turn", "args": {"degrees": 90}}]}
(from the `agent` node, or anything else); progress is published on `agent.status`.
Built in: stop, drive, turn, wait, say, and find. Without odometry, drive and turn are
timed from `speed_mps` and `turn_dps`, so calibrate those to your robot.
"""

from __future__ import annotations

import inspect
import itertools
import math
import sys
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, Iterable

from robot_core.live import ESTOP_TOPIC, LiveNode, NodeContext, Output
from robot_core.runtime import RuntimeMessage

SKILL_ATTR = "__nervlynx_skill__"
PLAN_TOPIC = "agent.plan"
STATUS_TOPIC = "agent.status"
SAY_TOPIC = "agent.say"
_TYPES = {int: "integer", float: "number", str: "string", bool: "boolean"}
_SPEECH_IDS = itertools.count(1)


def speech(text: str) -> dict[str, Any]:
  """An agent.say payload; `id` tells listeners (the dashboard, a speaker) it is new."""
  return {"text": str(text), "id": next(_SPEECH_IDS)}


@dataclass(frozen=True)
class SkillParam:
  name: str
  kind: type
  default: Any = inspect.Parameter.empty
  minimum: float | None = None
  maximum: float | None = None
  choices: tuple[Any, ...] | None = None

  @property
  def required(self) -> bool:
    return self.default is inspect.Parameter.empty

  def check(self, value: Any, skill: str) -> Any:
    """The value converted and range-checked, or ValueError explaining the limit."""
    try:
      if self.kind is bool:
        if not isinstance(value, bool):
          raise TypeError
        converted: Any = value
      elif self.kind in (int, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
          raise TypeError
        converted = self.kind(value)
      else:
        converted = str(value)
    except (TypeError, ValueError):
      raise ValueError(f"{skill}: {self.name} must be a {_TYPES.get(self.kind, 'value')}") from None
    if self.minimum is not None and converted < self.minimum or self.maximum is not None and converted > self.maximum:
      raise ValueError(f"{skill}: {self.name} must be between {self.minimum:g} and {self.maximum:g} (got {converted:g})")
    if self.choices is not None and converted not in self.choices:
      raise ValueError(f"{skill}: {self.name} must be one of {', '.join(map(str, self.choices))}")
    return converted

  def schema(self) -> dict[str, Any]:
    out: dict[str, Any] = {"type": _TYPES.get(self.kind, "string")}
    if self.minimum is not None:
      out["minimum"] = self.minimum
    if self.maximum is not None:
      out["maximum"] = self.maximum
    if self.choices is not None:
      out["enum"] = list(self.choices)
    if not self.required:
      out["default"] = self.default
    return out


@dataclass(frozen=True)
class SkillSpec:
  name: str
  fn: Callable[..., Any]
  description: str
  params: tuple[SkillParam, ...] = field(default=())

  def check_args(self, args: Any) -> dict[str, Any]:
    if args is None:
      args = {}
    if not isinstance(args, dict):
      raise ValueError(f"{self.name}: args must be an object")
    known = {p.name: p for p in self.params}
    unknown = sorted(set(args) - set(known))
    if unknown:
      raise ValueError(f"{self.name} has no argument {', '.join(unknown)} (it takes: {', '.join(known) or 'nothing'})")
    out = {}
    for param in self.params:
      if param.name in args:
        out[param.name] = param.check(args[param.name], self.name)
      elif param.required:
        raise ValueError(f"{self.name} needs {param.name}")
    return out

  def tool(self) -> dict[str, Any]:
    """This skill as an OpenAI-style function tool."""
    return {
      "type": "function",
      "function": {
        "name": self.name,
        "description": self.description,
        "parameters": {
          "type": "object",
          "properties": {p.name: p.schema() for p in self.params},
          "required": [p.name for p in self.params if p.required],
        },
      },
    }


SKILLS: dict[str, SkillSpec] = {}


def skill(target: Any = None, *, name: str | None = None, params: dict[str, Any] | None = None, description: str | None = None) -> Any:
  """Register a generator function as a skill. `params` bounds arguments:
  {"degrees": (-360, 360)} for a range, or {"label": ["person", "dog"]} for choices."""

  def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
    skill_name = name or fn.__name__
    bounds = params or {}
    signature = inspect.signature(fn)
    names = list(signature.parameters)
    if not names or names[0] != "robot":
      raise TypeError(f"@skill {skill_name}: the first parameter must be `robot`")
    try:
      hints = typing.get_type_hints(fn)
    except Exception:  # noqa: BLE001 - fall back to the defaults' types
      hints = {}
    specs = []
    for param in list(signature.parameters.values())[1:]:
      if param.kind is not inspect.Parameter.KEYWORD_ONLY:
        raise TypeError(f"@skill {skill_name}: arguments after robot must be keyword-only (put a * before them)")
      default = param.default
      annotation = hints.get(param.name)
      kind = annotation if isinstance(annotation, type) else (type(default) if default is not inspect.Parameter.empty and default is not None else float)
      if kind not in _TYPES:
        raise TypeError(f"@skill {skill_name}: {param.name} must be a number, string, or bool")
      bound = bounds.get(param.name)
      minimum = maximum = choices = None
      if isinstance(bound, tuple) and len(bound) == 2:
        minimum, maximum = float(bound[0]), float(bound[1])
      elif isinstance(bound, list):
        choices = tuple(bound)
      elif bound is not None:
        raise TypeError(f"@skill {skill_name}: params[{param.name!r}] must be (min, max) or a list of choices")
      specs.append(SkillParam(param.name, kind, default, minimum, maximum, choices))
    unknown = sorted(set(bounds) - {p.name for p in specs})
    if unknown:
      raise TypeError(f"@skill {skill_name}: params names unknown arguments: {', '.join(unknown)}")
    doc = description or (inspect.getdoc(fn) or skill_name.replace("_", " ")).strip().splitlines()[0]
    spec = SkillSpec(skill_name, fn, doc, tuple(specs))
    existing = SKILLS.get(skill_name)
    if existing is not None and existing.fn.__module__ != fn.__module__:
      where = getattr(sys.modules.get(existing.fn.__module__), "__file__", None) or existing.fn.__module__
      raise ValueError(f"skill {skill_name!r} is already defined in {where}; pick another name")
    SKILLS[skill_name] = spec
    setattr(fn, SKILL_ATTR, spec)
    return fn

  if target is not None:
    return wrap(target)
  return wrap


class SkillContext:
  """What a running skill sees: `robot` in `def my_skill(robot, *, ...)`."""

  def __init__(self, runner: SkillRunner, ctx: NodeContext) -> None:
    self._runner = runner
    self._ctx = ctx
    self.outputs: list[Output] = []

  @property
  def now_s(self) -> float:
    return self._ctx.now_ns / 1e9

  @property
  def odom(self) -> dict[str, Any] | None:
    return self._runner.latest.get(self._runner.odom_topic)

  @property
  def speed_mps(self) -> float:
    return self._runner.speed_mps

  @property
  def turn_dps(self) -> float:
    return self._runner.turn_dps

  @property
  def coast_s(self) -> float:
    """Roughly how long the robot keeps moving after a stop command (for stopping early)."""
    return self._runner.coast_s

  def detections(self, label: str | None = None) -> list[dict[str, Any]]:
    seen = self._runner.latest.get(self._runner.detections_topic) or {}
    found = seen.get("detections") or []
    return [d for d in found if label is None or d.get("label") == label]

  def drive(self, linear: float, angular: float = 0.0) -> None:
    limit = self._runner.max_speed
    self.outputs.append(
      (self._runner.drive_topic, "DriveCommand", {"linear": max(-limit, min(limit, float(linear))), "angular": max(-limit, min(limit, float(angular)))})
    )

  def stop(self) -> None:
    self.drive(0.0, 0.0)

  def send(self, topic: str, payload: dict[str, Any], schema: str = "Message") -> None:
    if topic == ESTOP_TOPIC:
      raise ValueError("skills cannot publish on safety.estop")
    self.outputs.append((topic, schema, dict(payload)))

  def say(self, text: str) -> None:
    self.outputs.append((SAY_TOPIC, "Speech", speech(text)))

  def wait(self, seconds: float) -> Generator[None, None, None]:
    end = self.now_s + max(0.0, float(seconds))
    while self.now_s < end:
      yield


# ---------------------------------------------------------------------------- built-in skills


@skill(name="stop", description="Stop moving now and forget the rest of the plan.")
def _stop(robot):
  robot.stop()
  return
  yield  # pragma: no cover - makes this a generator


def _await_odom(robot, seconds: float = 0.25):
  """Give odometry a moment to arrive, so a plan sent at start-up still uses it."""
  end = robot.now_s + seconds
  while robot.odom is None and robot.now_s < end:
    robot.stop()
    yield


@skill(name="drive", params={"distance_m": (-3.0, 3.0), "speed": (0.1, 1.0)})
def _drive(robot, *, distance_m: float, speed: float = 0.4):
  """Drive straight ahead this many metres (negative drives backwards)."""
  direction = 1.0 if distance_m >= 0 else -1.0
  goal = abs(distance_m)
  yield from _await_odom(robot)
  odom = robot.odom
  expected = goal / max(1e-6, robot.speed_mps * speed)
  deadline = robot.now_s + expected * 2.0 + 2.0
  if odom is not None and "distance_m" in odom:
    start = float(odom["distance_m"])
    while robot.now_s < deadline:
      now = robot.odom
      travelled = float(now["distance_m"]) - start
      if travelled + abs(float(now.get("speed_mps", 0.0))) * robot.coast_s >= goal:
        break  # it will coast the rest of the way
      robot.drive(direction * speed * max(0.4, min(1.0, (goal - travelled) / 0.2)))
      yield
  else:
    end = robot.now_s + expected
    while robot.now_s < end:
      robot.drive(direction * speed)
      yield
  robot.stop()
  return f"drove {distance_m:g} m"


@skill(name="turn", params={"degrees": (-360.0, 360.0), "speed": (0.1, 1.0)})
def _turn(robot, *, degrees: float, speed: float = 0.5):
  """Turn in place this many degrees; positive is left (counter-clockwise)."""
  direction = 1.0 if degrees >= 0 else -1.0
  goal = abs(degrees)
  expected = goal / max(1e-6, robot.turn_dps * speed)
  yield from _await_odom(robot)
  deadline = robot.now_s + expected * 2.0 + 2.0
  odom = robot.odom
  if odom is not None and "heading_deg" in odom:
    last, turned = float(odom["heading_deg"]), 0.0
    while robot.now_s < deadline:
      rate = abs(float(robot.odom.get("yaw_rate_dps", 0.0)))
      if turned + rate * robot.coast_s >= goal:
        break  # it will coast the rest of the way
      robot.drive(0.0, direction * speed * max(0.35, min(1.0, (goal - turned) / 40.0)))
      yield
      heading = float(robot.odom["heading_deg"])
      turned += abs((heading - last + 180.0) % 360.0 - 180.0)
      last = heading
  else:
    end = robot.now_s + expected
    while robot.now_s < end:
      robot.drive(0.0, direction * speed)
      yield
  robot.stop()
  return f"turned {degrees:g} degrees"


@skill(name="wait", params={"seconds": (0.0, 30.0)})
def _wait(robot, *, seconds: float):
  """Stand still for this many seconds."""
  robot.stop()
  yield from robot.wait(seconds)
  return f"waited {seconds:g} s"


@skill(name="say")
def _say(robot, *, text: str):
  """Say something out loud (on the dashboard, and on the robot if it has a speaker)."""
  robot.say(text)
  return
  yield  # pragma: no cover


@skill(name="find", params={"timeout_s": (1.0, 30.0), "speed": (0.1, 0.8)})
def _find(robot, *, label: str = "person", timeout_s: float = 10.0, speed: float = 0.35):
  """Turn until something with this label (for example person) is straight ahead."""
  end = robot.now_s + timeout_s
  while robot.now_s < end:
    seen = robot.detections(label)
    if seen:
      x = max(seen, key=lambda d: d["size"][1])["center"][0]
      if abs(x - 0.5) < 0.08:
        robot.stop()
        return f"found a {label}"
      robot.drive(0.0, max(-speed, min(speed, 1.5 * (0.5 - x))))
    else:
      robot.drive(0.0, speed)
    yield
  robot.stop()
  return f"could not find a {label}"


# ---------------------------------------------------------------------------- runner


def parse_plan(payload: Any, *, max_steps: int) -> list[tuple[SkillSpec, dict[str, Any]]]:
  """Checked (skill, args) steps, or ValueError saying what is wrong with the plan."""
  steps = payload.get("steps") if isinstance(payload, dict) else None
  if not isinstance(steps, list) or not steps:
    raise ValueError('a plan needs steps: [{"skill": "turn", "args": {"degrees": 90}}]')
  if len(steps) > max_steps:
    raise ValueError(f"plans are limited to {max_steps} steps (got {len(steps)})")
  checked = []
  for step in steps:
    if not isinstance(step, dict) or not isinstance(step.get("skill"), str):
      raise ValueError('each step needs a skill name, e.g. {"skill": "stop"}')
    spec = SKILLS.get(step["skill"])
    if spec is None:
      raise ValueError(f"there is no skill called {step['skill']!r} (skills: {', '.join(sorted(SKILLS))})")
    checked.append((spec, spec.check_args(step.get("args"))))
  return checked


class SkillRunner(LiveNode):
  """Runs plans of skills one step at a time and reports progress on agent.status."""

  rate_hz = 20.0

  def __init__(
    self,
    *,
    max_speed: float = 0.6,
    speed_mps: float = 0.5,
    turn_dps: float = 120.0,
    drive_topic: str = "cmd.drive",
    odom_topic: str = "odom",
    detections_topic: str = "detections.front",
    max_steps: int = 10,
    max_plan_s: float = 180.0,
    coast_s: float = 0.25,
  ) -> None:
    """`speed_mps` and `turn_dps` are how fast the robot moves and turns at full speed;
    drive and turn use them when there is no odometry. With odometry they stop
    `coast_s` early at the current speed, since a robot keeps rolling after a stop."""
    if not 0 < max_speed <= 1:
      raise ValueError("max_speed must be in (0, 1]")
    if speed_mps <= 0 or turn_dps <= 0 or max_steps < 1 or max_plan_s <= 0:
      raise ValueError("speed_mps, turn_dps, max_steps, and max_plan_s must be > 0")
    self.max_speed = float(max_speed)
    self.speed_mps = float(speed_mps)
    self.turn_dps = float(turn_dps)
    self.drive_topic = drive_topic
    self.odom_topic = odom_topic
    self.detections_topic = detections_topic
    self.max_steps = int(max_steps)
    self.max_plan_ns = int(max_plan_s * 1e9)
    self.coast_s = max(0.0, float(coast_s))
    self.input_topics = (PLAN_TOPIC, odom_topic, detections_topic, drive_topic, ESTOP_TOPIC)
    self.latest: dict[str, dict[str, Any]] = {}
    self._queue: list[tuple[SkillSpec, dict[str, Any]]] = []
    self._current: tuple[SkillSpec, dict[str, Any]] | None = None
    self._gen: Generator[None, None, Any] | None = None
    self._robot: SkillContext | None = None
    self._plan_started_ns = 0
    self._results: list[str] = []
    self._status: dict[str, Any] = {"state": "idle", "message": "ready"}
    self._pending: list[Output] = []
    self._name = "skills"
    self.plans = 0

  def setup(self, ctx: NodeContext) -> None:
    self._name = ctx.name

  def _report(self, state: str, message: str) -> None:
    current = {"skill": self._current[0].name, "args": self._current[1]} if self._current else None
    self._status = {
      "state": state,
      "message": message,
      "current": current,
      "queue": [{"skill": spec.name, "args": args} for spec, args in self._queue],
      "done": list(self._results),
    }
    self._pending.append((STATUS_TOPIC, "SkillStatus", dict(self._status)))

  def _cancel(self, message: str, *, state: str = "stopped") -> None:
    was_moving = self._current is not None
    self._queue.clear()
    self._current = None
    self._gen = None
    if was_moving:
      self._pending.append((self.drive_topic, "DriveCommand", {"linear": 0.0, "angular": 0.0}))
    self._report(state, message)

  def on_message(self, msg: RuntimeMessage, ctx: NodeContext) -> Iterable[Output] | None:
    topic = msg.envelope.topic
    if topic == PLAN_TOPIC:
      self._plan(msg.payload, ctx)
    elif topic == ESTOP_TOPIC:
      if msg.payload.get("engaged") and (self._current or self._queue):
        self._cancel("stopped: the e-stop was pressed")
    elif topic == self.drive_topic:
      if msg.envelope.source != self._name and self._current is not None:
        self._cancel(f"stopped: {msg.envelope.source} took over the driving")
    else:
      self.latest[topic] = msg.payload
    return self._flush()

  def _plan(self, payload: dict[str, Any], ctx: NodeContext) -> None:
    try:
      steps = parse_plan(payload, max_steps=self.max_steps)
    except ValueError as exc:
      self._report("rejected", str(exc))
      return
    if ctx.estop_engaged:
      self._report("rejected", "the e-stop is latched; clear it first")
      return
    if self._current is not None:
      self._cancel("replaced by a new plan")
    self._queue = steps
    self._results = []
    self._plan_started_ns = ctx.now_ns
    self.plans += 1
    self._next(ctx)

  def _next(self, ctx: NodeContext) -> None:
    if not self._queue:
      self._current, self._gen = None, None
      self._report("done", "; ".join(self._results) or "done")
      return
    spec, args = self._queue.pop(0)
    self._current = (spec, args)
    robot = SkillContext(self, ctx)
    self._gen = spec.fn(robot, **args)
    self._robot = robot
    self._report("running", f"{spec.name} {', '.join(f'{k}={v}' for k, v in args.items())}".strip())
    self._advance(ctx)

  def _advance(self, ctx: NodeContext) -> None:
    assert self._gen is not None and self._current is not None and self._robot is not None
    robot = self._robot
    robot._ctx = ctx
    robot.outputs = []
    try:
      next(self._gen)
    except StopIteration as done:
      self._pending.extend(robot.outputs)
      self._results.append(str(done.value) if done.value else self._current[0].name)
      self._next(ctx)
      return
    except Exception as exc:  # noqa: BLE001 - a broken skill stops the plan, not the robot
      self._pending.extend(robot.outputs)
      self._cancel(f"{self._current[0].name} failed: {type(exc).__name__}: {exc}", state="failed")
      return
    self._pending.extend(robot.outputs)

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    if self._current is not None:
      if ctx.estop_engaged:
        self._cancel("stopped: the e-stop was pressed")
      elif ctx.now_ns - self._plan_started_ns > self.max_plan_ns:
        self._cancel(f"stopped: the plan ran longer than {self.max_plan_ns / 1e9:.0f} s")
      else:
        self._advance(ctx)
    return self._flush()

  def _flush(self) -> list[Output] | None:
    out, self._pending = self._pending, []
    return out or None

  def status(self) -> dict[str, Any]:
    return {"skills": sorted(SKILLS), "plans": self.plans, **self._status}
