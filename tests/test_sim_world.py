import math

import pytest

from robot_core.live import LiveRuntime
from robot_core.runtime import SimulatedClock
from robot_core.sim import SimWorld, SkidSteerSim

ARENA = {"width_m": 4.0, "height_m": 3.0, "obstacles": [{"circle": [3.0, 1.5, 0.25]}, {"box": [0.5, 2.2, 1.0, 2.8]}]}


def test_rays_hit_walls_circles_and_boxes() -> None:
  world = SimWorld.from_dict(ARENA)
  assert world.ray(2.0, 1.5, 0.0, 10.0) == pytest.approx(0.75)
  assert world.ray(2.0, 1.5, math.pi, 10.0) == pytest.approx(2.0)
  assert world.ray(0.75, 1.0, math.pi / 2, 10.0) == pytest.approx(1.2)
  assert world.ray(2.0, 1.5, 0.0, 0.5) == 0.5
  assert world.ray(3.0, 1.5, 0.0, 10.0) == 0.0


def test_collisions_cover_walls_and_obstacles() -> None:
  world = SimWorld.from_dict(ARENA)
  assert world.collision(2.0, 1.5, 0.12) is None
  assert world.collision(0.05, 1.5, 0.12) == "a wall"
  assert world.collision(2.8, 1.5, 0.12) == "obstacle 0"
  assert world.collision(0.75, 2.1, 0.12) == "obstacle 1"


def run_plant(plant: SkidSteerSim, left: float, right: float, seconds: float) -> LiveRuntime:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("plant", plant, rate_hz=50)
  rt.start()
  rt.publish_external("drive.state", "DriveState", {"left_applied": left, "right_applied": right})
  for _ in range(int(seconds * 50)):
    rt.clock.advance_ms(20)
    rt.step()
  return rt


def test_driving_into_an_obstacle_stops_the_robot_and_counts_one_collision() -> None:
  plant = SkidSteerSim(world=ARENA, start=[1.5, 1.5, 0.0])
  rt = run_plant(plant, 0.8, 0.8, seconds=5.0)
  assert plant.collisions == 1 and plant.bumped == "obstacle 0"
  assert 3.0 - 0.25 - 0.12 - 0.05 <= plant.x_m < 3.0 - 0.25 - 0.12
  assert plant.wheel_speed == [0.0, 0.0]
  assert any(e.kind == "sim_collision" and "obstacle 0" in e.message for e in rt.fault_events)
  assert plant.status()["collisions"] == 1 and plant.status()["world"]["width_m"] == 4.0


def test_range_sensors_see_the_obstacle_get_closer() -> None:
  seen: list = []
  plant = SkidSteerSim(
    world=ARENA,
    start=[1.0, 1.5, 0.0],
    range_sensors=[{"name": "front", "max_range_m": 1.5}, {"name": "left", "angle_deg": 90, "max_range_m": 0.5}],
  )
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("plant", plant, rate_hz=50)
  rt.subscribe("probe", "range.front", seen.append)
  left: list = []
  rt.subscribe("probe_left", "range.left", left.append)
  rt.start()
  rt.publish_external("drive.state", "DriveState", {"left_applied": 0.6, "right_applied": 0.6})
  for _ in range(100):
    rt.clock.advance_ms(20)
    rt.step()
  distances = [msg.payload["distance_m"] for msg in seen]
  assert distances[0] == 1.5 and seen[0].payload["hit"] is False
  assert distances[-1] < 1.2 and seen[-1].payload["hit"] is True
  assert all(b <= a + 1e-9 for a, b in zip(distances, distances[1:]))
  assert left[-1].payload == {"distance_m": 0.5, "max_range_m": 0.5, "hit": False}


def test_sensor_noise_is_repeatable_for_a_seed() -> None:
  def readings(seed: int) -> list[float]:
    plant = SkidSteerSim(world=ARENA, start=[1.0, 1.5, 0.0], range_sensors=[{"name": "front", "noise_m": 0.02}], seed=seed)
    run_plant(plant, 0.0, 0.0, seconds=0.5)
    return [plant.ranges["front"]]

  assert readings(3) == readings(3)
  assert readings(3) != readings(4)


def test_world_configuration_errors_are_explained() -> None:
  with pytest.raises(ValueError, match="range_sensors need a world"):
    SkidSteerSim(range_sensors=[{"name": "front"}])
  with pytest.raises(ValueError, match="inside obstacle 0"):
    SkidSteerSim(world=ARENA, start=[3.0, 1.5, 0.0])
  with pytest.raises(ValueError, match="circle"):
    SkidSteerSim(world={"obstacles": [{"triangle": [0, 0, 1]}]})
  with pytest.raises(ValueError, match="unique"):
    SkidSteerSim(world=ARENA, range_sensors=[{"name": "a"}, {"name": "a"}])
  with pytest.raises(ValueError, match="start needs a world"):
    SkidSteerSim(start=[0.0, 0.0, 0.0])
