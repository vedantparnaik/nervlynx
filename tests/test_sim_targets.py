import json

import pytest

from robot_core.live import LiveRuntime
from robot_core.runtime import SimulatedClock
from robot_core.sim import SimTarget, SimWorld, SkidSteerSim


def test_targets_walk_their_path_and_back() -> None:
  person = SimTarget.parse({"label": "person", "path": [[1, 1], [3, 1]], "speed_mps": 0.5}, 0)
  assert person.position == (1, 1)
  person.advance(2.0)
  assert person.position == pytest.approx((2.0, 1.0))
  person.advance(3.0)
  assert person.position == pytest.approx((2.5, 1.0))  # walked to the end and half way back
  square = SimTarget.parse({"path": [[0, 0], [1, 0], [1, 1], [0, 1]], "speed_mps": 1.0}, 0)
  square.advance(3.5)
  assert square.position == pytest.approx((0.0, 0.5))  # closed loop
  still = SimTarget.parse({"label": "dog", "at": [2, 2]}, 1)
  still.advance(10)
  assert still.position == (2, 2) and still.height_m == 0.5
  with pytest.raises(ValueError, match=r"world.targets\[0\] needs at"):
    SimTarget.parse({"label": "person"}, 0)
  with pytest.raises(ValueError, match="unknown fields speed"):
    SimTarget.parse({"at": [1, 1], "speed": 1}, 0)


def test_targets_block_the_robot_and_its_sensors() -> None:
  world = SimWorld.from_dict({"width_m": 4, "height_m": 3, "targets": [{"label": "person", "at": [3.0, 1.5]}]})
  assert world.ray(1.0, 1.5, 0.0, 10.0) == pytest.approx(1.8)
  assert world.ray(1.0, 1.5, 0.0, 10.0, ignore=world.targets[0]) == pytest.approx(3.0)
  assert world.collision(2.75, 1.5, 0.1) == "the person"
  assert world.to_dict()["targets"] == [{"label": "person", "x_m": 3.0, "y_m": 1.5, "radius_m": 0.2}]
  with pytest.raises(ValueError, match="puts the robot inside the person"):
    SkidSteerSim(world={"targets": [{"at": [2, 1.5]}]}, start=[2, 1.5, 0])


def camera_sim(targets, start=(1.0, 1.5, 0.0), obstacles=()) -> tuple[LiveRuntime, SkidSteerSim, list]:
  sim = SkidSteerSim(
    world={"width_m": 8, "height_m": 3, "targets": targets, "obstacles": list(obstacles)},
    start=list(start),
    cameras=[{"name": "front", "fov_deg": 60}],
  )
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("sim", sim, rate_hz=50)
  seen: list = []
  rt.subscribe("probe", "detections.front", seen.append)
  return rt, sim, seen


def test_the_simulated_camera_sees_people_like_the_detector_does() -> None:
  rt, sim, seen = camera_sim([{"label": "person", "at": [4.0, 2.0]}, {"label": "dog", "at": [1.0, 2.9]}])
  rt.run(duration_s=0.5)
  payload = seen[-1].payload
  assert payload["backend"] == "sim" and (payload["width"], payload["height"]) == (640, 480)
  assert [d["label"] for d in payload["detections"]] == ["person"]  # the dog is outside the field of view
  person = payload["detections"][0]
  assert person["center"][0] < 0.5  # to the left of centre, as the target is to the robot's left
  assert 0.2 < person["size"][1] < 0.8 and person["box"][3] > 0.5
  json.dumps(payload)
  near_rt, _, near = camera_sim([{"label": "person", "at": [2.2, 1.5]}])
  near_rt.run(duration_s=0.2)
  assert near[-1].payload["detections"][0]["size"][1] > person["size"][1]  # closer looks bigger
  assert abs(near[-1].payload["detections"][0]["center"][0] - 0.5) < 0.01


def test_walls_and_obstacles_hide_targets() -> None:
  rt, _, seen = camera_sim([{"label": "person", "at": [5.0, 1.5]}], obstacles=[{"box": [2.5, 1.0, 3.0, 2.0]}])
  rt.run(duration_s=0.2)
  assert seen[-1].payload["detections"] == []


def test_people_wait_for_the_robot_instead_of_walking_into_it() -> None:
  rt, sim, _ = camera_sim([{"label": "person", "path": [[3.0, 1.5], [0.5, 1.5]], "speed_mps": 0.5}], start=(1.6, 1.5, 0.0))
  rt.run(duration_s=6.0)
  tx, _ = sim.world.targets[0].position
  assert sim.collisions == 0 and tx == pytest.approx(1.6 + 0.12 + 0.2 + 0.15, abs=0.02)
  assert sim.status()["cameras"] == [{"name": "front", "fov_deg": 60.0, "angle_deg": 0.0, "range_m": 6.0}]
