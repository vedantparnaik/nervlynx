import pytest

from robot_core.live import LiveRuntime
from robot_core.live_config import build_live_runtime
from robot_core.project import build_registry, load_project
from robot_core.runtime import SimulatedClock
from robot_core.skills import SKILLS, SkillRunner, parse_plan, skill

MOTORS = {"left": [{"name": "left", "rpwm": 1, "lpwm": 2}], "right": [{"name": "right", "rpwm": 3, "lpwm": 4}]}


def test_skills_describe_themselves_as_bounded_tools() -> None:
  tool = SKILLS["turn"].tool()["function"]
  assert tool["name"] == "turn" and tool["description"].startswith("Turn in place")
  assert tool["parameters"]["properties"]["degrees"] == {"type": "number", "minimum": -360.0, "maximum": 360.0}
  assert tool["parameters"]["required"] == ["degrees"]
  assert SKILLS["find"].tool()["function"]["parameters"]["properties"]["label"] == {"type": "string", "default": "person"}
  assert {"stop", "drive", "turn", "wait", "say", "find"} <= set(SKILLS)


def test_plans_are_checked_before_anything_moves() -> None:
  steps = parse_plan({"steps": [{"skill": "turn", "args": {"degrees": 90}}, {"skill": "drive", "args": {"distance_m": 1}}]}, max_steps=5)
  assert [(spec.name, args) for spec, args in steps] == [("turn", {"degrees": 90.0}), ("drive", {"distance_m": 1.0})]
  for plan, message in (
    ({"steps": []}, "a plan needs steps"),
    ({"steps": [{"skill": "fly"}]}, "there is no skill called 'fly'"),
    ({"steps": [{"skill": "drive", "args": {"distance_m": 10}}]}, r"distance_m must be between -3 and 3 \(got 10\)"),
    ({"steps": [{"skill": "drive"}]}, "drive needs distance_m"),
    ({"steps": [{"skill": "turn", "args": {"degrees": "left"}}]}, "degrees must be a number"),
    ({"steps": [{"skill": "wait", "args": {"seconds": 1, "loudly": True}}]}, "wait has no argument loudly"),
    ({"steps": [{"skill": "stop"}] * 6}, "limited to 5 steps"),
  ):
    with pytest.raises(ValueError, match=message):
      parse_plan(plan, max_steps=5)


def test_skill_definitions_are_checked() -> None:
  with pytest.raises(TypeError, match="first parameter must be `robot`"):
    skill(lambda speed: None)
  with pytest.raises(TypeError, match="keyword-only"):

    @skill
    def bad(robot, speed=1.0):
      yield

  with pytest.raises(ValueError, match="skill 'drive' is already defined"):

    @skill(name="drive")
    def my_drive(robot, *, distance_m=1.0):
      yield


def sim_robot(**runner) -> LiveRuntime:
  cfg = {
    "nodes": [
      {"name": "drive", "plugin": "skid_steer_drive", "rate_hz": 50, "params": MOTORS},
      {"name": "sim", "plugin": "skid_steer_sim", "rate_hz": 50, "params": {"world": {"width_m": 8, "height_m": 8}, "start": [4, 4, 0], "odom_every_n_ticks": 2}},
      {"name": "skills", "plugin": "skills", "params": runner},
    ]
  }
  rt = build_live_runtime(cfg, build_registry(), clock=SimulatedClock())
  rt.start()
  return rt


def run(rt: LiveRuntime, seconds: float) -> None:
  for _ in range(int(round(seconds / 0.02))):
    rt.clock.advance_ms(20)
    rt.step()


def test_turn_then_drive_uses_odometry_in_the_simulator() -> None:
  rt = sim_robot()
  rt.publish_external("agent.plan", "Plan", {"steps": [{"skill": "turn", "args": {"degrees": 90}}, {"skill": "drive", "args": {"distance_m": 1.0}}]})
  run(rt, 12.0)
  status = rt.nodes["skills"].status()
  assert status["state"] == "done" and status["done"] == ["turned 90 degrees", "drove 1 m"]
  odom = rt.nodes["sim"].odometry()
  assert 80 < odom["heading_deg"] < 115
  assert odom["y_m"] - 4.0 == pytest.approx(1.05, abs=0.2)


def test_without_odometry_drive_is_timed_from_speed_mps() -> None:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("skills", SkillRunner(speed_mps=0.5, max_speed=0.6))
  commands: list = []
  rt.subscribe("probe", "cmd.drive", commands.append)
  rt.start()
  rt.publish_external("agent.plan", "Plan", {"steps": [{"skill": "drive", "args": {"distance_m": -1.0, "speed": 1.0}}]})
  run(rt, 3.0)
  moving = [m.payload for m in commands if m.payload["linear"] != 0.0]
  assert moving[0] == {"linear": -0.6, "angular": 0.0}  # clamped to max_speed
  assert len(moving) == pytest.approx(40, abs=2)  # 1 m at 0.5 m/s is 2 s at 20 Hz
  assert commands[-1].payload == {"linear": 0.0, "angular": 0.0}


def test_estop_and_operator_takeover_cancel_the_plan() -> None:
  rt = sim_robot()
  rt.publish_external("agent.plan", "Plan", {"steps": [{"skill": "drive", "args": {"distance_m": 2.0}}, {"skill": "turn", "args": {"degrees": 90}}]})
  run(rt, 1.0)
  rt.request_estop("test", source="test")
  run(rt, 0.1)
  status = rt.nodes["skills"].status()
  assert status["state"] == "stopped" and "e-stop" in status["message"] and status["queue"] == []
  rt.publish_external("agent.plan", "Plan", {"steps": [{"skill": "wait", "args": {"seconds": 1}}]})
  run(rt, 0.1)
  assert rt.nodes["skills"].status()["state"] == "rejected"
  rt.request_estop_clear(source="test")
  run(rt, 0.1)
  rt.publish_external("agent.plan", "Plan", {"steps": [{"skill": "turn", "args": {"degrees": 180}}]})
  run(rt, 0.5)
  rt.publish_external("cmd.drive", "DriveCommand", {"linear": 0.0, "angular": 0.0}, source="http")
  run(rt, 0.1)
  assert rt.nodes["skills"].status()["message"] == "stopped: http took over the driving"


def test_find_turns_until_the_person_is_ahead() -> None:
  cfg = {
    "nodes": [
      {"name": "drive", "plugin": "skid_steer_drive", "rate_hz": 50, "params": MOTORS},
      {
        "name": "sim",
        "plugin": "skid_steer_sim",
        "rate_hz": 50,
        "params": {"world": {"width_m": 8, "height_m": 8, "targets": [{"at": [4, 6.5]}]}, "start": [4, 4, 0], "cameras": [{"name": "front"}]},
      },
      {"name": "skills", "plugin": "skills"},
    ]
  }
  rt = build_live_runtime(cfg, build_registry(), clock=SimulatedClock())
  rt.start()
  rt.publish_external("agent.plan", "Plan", {"steps": [{"skill": "find", "args": {"label": "person"}}]})
  run(rt, 10.0)
  assert rt.nodes["skills"].status()["done"] == ["found a person"]
  assert 75 < rt.nodes["sim"].odometry()["heading_deg"] < 105


def test_project_skills_come_from_nodes_files(tmp_path) -> None:
  (tmp_path / "nodes").mkdir()
  (tmp_path / "nodes" / "arm.py").write_text(
    "from nervlynx import skill\n\n"
    "@skill(params={'times': (1, 5)})\n"
    "def wave_arm(robot, *, times=2):\n"
    "  '''Wave the arm.'''\n"
    "  for _ in range(times):\n"
    "    robot.send('cmd.servo', {'arm': 150})\n"
    "    yield from robot.wait(0.2)\n"
    "  return f'waved {times} times'\n",
    encoding="utf-8",
  )
  (tmp_path / "robot.yaml").write_text("nodes:\n  - {plugin: skills}\n", encoding="utf-8")
  cfg, reg, problems = load_project(tmp_path / "robot.yaml")
  assert problems == [] and SKILLS["wave_arm"].description == "Wave the arm."
  rt = build_live_runtime(cfg, reg, clock=SimulatedClock())
  servo: list = []
  rt.subscribe("probe", "cmd.servo", servo.append)
  rt.start()
  rt.publish_external("agent.plan", "Plan", {"steps": [{"skill": "wave_arm", "args": {"times": 3}}]})
  run(rt, 1.0)
  assert len(servo) == 3 and rt.nodes["skills"].status()["done"] == ["waved 3 times"]
  load_project(tmp_path / "robot.yaml")  # loading the same project again is fine
