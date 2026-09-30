import struct
import sys
import threading
import types
from pathlib import Path

import pytest
import yaml

from robot_core.drive import SkidSteerDrive
from robot_core.live import LiveNode, LiveRuntime, LiveRuntimeError, supports_calibration
from robot_core.live_config import build_live_runtime
from robot_core.overlay import write_calibration
from robot_core.project import load_project
from robot_core.runtime import SimulatedClock
from robot_core.sensors import GRAVITY_MPS2, MPU6050Imu, format_axes, map_axes, parse_axes


def advance(rt: LiveRuntime, seconds: float) -> None:
  for _ in range(int(round(seconds / 0.02))):
    rt.clock.advance_ms(20)
    rt.step()


def call(rt: LiveRuntime, name: str, action: str, **args):
  """Run a calibration action the way the dashboard does: from another thread."""
  out: dict = {}

  def worker() -> None:
    try:
      out["result"] = rt.call_node(name, lambda node, ctx: node.calibrate(action, args, ctx), timeout_s=2.0)
    except Exception as exc:  # noqa: BLE001 - handed to the test
      out["error"] = exc

  thread = threading.Thread(target=worker)
  thread.start()
  while thread.is_alive():
    rt.step()
    thread.join(0.001)
  if "error" in out:
    raise out["error"]
  return out["result"]


def drive_runtime(**params) -> tuple[LiveRuntime, SkidSteerDrive]:
  drive = SkidSteerDrive(
    driver="l298n",
    left=[{"name": "left", "in1": 5, "in2": 6, "en": 12}],
    right=[{"name": "right", "in1": 20, "in2": 21, "en": 13}],
    max_speed=0.6,
    **params,
  )
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("drive", drive, rate_hz=50)
  rt.start()
  return rt, drive


def test_call_node_runs_on_the_executor_and_hands_back_errors() -> None:
  rt, _ = drive_runtime()
  seen: list = []
  out = call(rt, "drive", "describe")
  assert out["kind"] == "drive" and out["sides"]["left"] == [{"name": "left", "invert": False}]
  with pytest.raises(ValueError, match="unknown drive calibration action"):
    call(rt, "drive", "dance")
  with pytest.raises(LiveRuntimeError, match="no node named 'arm'"):
    rt.call_node("arm", lambda node, ctx: seen.append(node))
  with pytest.raises(TimeoutError, match="did not answer"):
    rt.call_node("drive", lambda node, ctx: None, timeout_s=0.05)
  assert supports_calibration(SkidSteerDrive(left=[{"rpwm": 1, "lpwm": 2}], right=[{"rpwm": 3, "lpwm": 4}]))
  assert not supports_calibration(LiveNode())


def test_spin_test_moves_one_motor_then_stops_by_itself() -> None:
  rt, drive = drive_runtime()
  call(rt, "drive", "spin", motor="right", duty=0.9, seconds=0.5)
  advance(rt, 0.1)
  motors = {m.name: m for m in drive.left_motors + drive.right_motors}
  assert motors["right"].duty == 0.6 and motors["left"].duty == 0.0  # capped at max_speed
  assert drive.status()["testing"] == "spin right forward at 0.60"
  advance(rt, 0.5)
  assert motors["right"].duty == 0.0 and drive.status()["testing"] is None
  advance(rt, 0.5)
  assert motors["right"].duty == 0.0  # no stale command resumes afterwards


def test_tests_respect_the_estop_and_stop_on_a_real_command() -> None:
  rt, drive = drive_runtime()
  rt.request_estop("test", source="test")
  advance(rt, 0.04)
  with pytest.raises(ValueError, match="clear the e-stop first"):
    call(rt, "drive", "test", move="forward")
  rt.request_estop_clear(source="test")
  advance(rt, 0.04)
  call(rt, "drive", "test", move="left", duty=0.4, seconds=2.0)
  advance(rt, 0.1)
  assert [m.duty for m in drive.left_motors + drive.right_motors] == [-0.4, 0.4]
  rt.publish_external("cmd.drive", "DriveCommand", {"left": 0.0, "right": 0.0})
  advance(rt, 0.04)
  assert drive.status()["testing"] is None
  assert any(e.kind == "calibration" for e in rt.fault_events)
  call(rt, "drive", "test", move="forward", seconds=2.0)
  advance(rt, 0.1)
  rt.request_estop("stop", source="test")
  advance(rt, 0.04)
  assert drive.status()["testing"] is None and all(m.duty == 0.0 for m in drive.left_motors + drive.right_motors)


def test_invert_swap_and_tuning_change_live_and_save_only_what_changed(tmp_path: Path) -> None:
  rt, drive = drive_runtime()
  assert drive.calibration() is None
  out = call(rt, "drive", "invert", motor="right", value=True)
  assert out["sides"]["right"] == [{"name": "right", "invert": True}]
  out = call(rt, "drive", "swap_sides", value=True)
  assert out["sides"]["left"][0]["name"] == "right" and out["swap_sides"] is True
  call(rt, "drive", "tuning", min_duty=0.24)
  assert drive.tuning.min_duty == 0.24 and drive._left.tuning.min_duty == 0.24
  with pytest.raises(ValueError, match="tuning.min_duty must be in"):
    call(rt, "drive", "tuning", min_duty=1.5)
  patch = drive.calibration()
  assert patch == {"right": [{"name": "right", "invert": True}], "swap_sides": True, "tuning": {"min_duty": 0.24}}

  config = tmp_path / "robot.yaml"
  config.write_text(
    yaml.safe_dump(
      {
        "nodes": [
          {
            "name": "drive",
            "plugin": "skid_steer_drive",
            "rate_hz": 50,
            "params": {
              "driver": "l298n",
              "left": [{"name": "left", "in1": 5, "in2": 6, "en": 12}],
              "right": [{"name": "right", "in1": 20, "in2": 21, "en": 13}],
            },
          }
        ]
      }
    ),
    encoding="utf-8",
  )
  write_calibration(config, {"drive": {"params": patch}})
  cfg, reg, problems = load_project(config)
  assert problems == []
  built = build_live_runtime(cfg, reg, clock=SimulatedClock()).nodes["drive"]
  built.setup(types.SimpleNamespace(metrics=rt.metrics, fault=lambda *a, **k: None, now_ns=0))
  assert [m.name for m in built.left_motors] == ["right"] and built.left_motors[0].invert is True
  assert built.tuning.min_duty == 0.24


def test_axes_are_validated_and_mapped() -> None:
  axes = parse_axes(["-y", "+x", "+z"])
  assert map_axes([1.0, 2.0, 3.0], axes) == [-2.0, 1.0, 3.0]
  assert format_axes(axes) == ["-y", "+x", "+z"]
  with pytest.raises(ValueError, match="mirror image"):
    parse_axes(["+x", "+y", "-z"])
  with pytest.raises(ValueError, match="each of x, y, and z once"):
    parse_axes(["+x", "+x", "+z"])
  with pytest.raises(ValueError, match="forward, left, and up"):
    parse_axes(["x", "y"])


class TiltingBus:
  """An MPU6050 whose accelerometer reading (in g, IMU frame) the test sets."""

  def __init__(self):
    self.accel_g = [0.0, 0.0, 1.0]

  def read_byte_data(self, address, register):
    return 0x68

  def write_byte_data(self, address, register, value):
    pass

  def read_i2c_block_data(self, address, register, length):
    words = [int(round(g * 16384)) for g in self.accel_g] + [0, 0, 0, 0]
    return list(struct.pack(">7h", *words))

  def close(self):
    pass


def test_imu_wizard_finds_the_mounting_from_two_poses(monkeypatch) -> None:
  bus = TiltingBus()
  monkeypatch.setitem(sys.modules, "smbus2", types.SimpleNamespace(SMBus=lambda number: bus))
  imu = MPU6050Imu(backend="gpiozero", calibrate_samples=0)
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("imu", imu)
  rt.start()
  with pytest.raises(ValueError, match="capture the flat pose first"):
    call(rt, "imu", "nose_up")
  advance(rt, 0.3)
  out = call(rt, "imu", "flat")
  assert out["step"] == "nose_up" and out["message"].startswith("up is IMU +z")
  # Mounted with IMU -y pointing forward; lifting the nose 30 degrees tips gravity onto it.
  bus.accel_g = [0.0, -0.5, 0.866]
  advance(rt, 0.6)
  out = call(rt, "imu", "nose_up")
  assert out["axes"] == ["-y", "+x", "+z"] and out["message"] == "forward is IMU -y, left is +x, up is +z"
  assert imu.calibration() == {"axes": ["-y", "+x", "+z"]}
  bus.accel_g = [0.0, -0.5, 0.866]
  advance(rt, 0.1)
  assert imu.last["accel_mps2"] == pytest.approx([0.5 * GRAVITY_MPS2, 0.0, 0.866 * GRAVITY_MPS2], abs=1e-3)


def test_imu_wizard_explains_a_missed_tilt() -> None:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("imu", MPU6050Imu())
  rt.start()
  advance(rt, 0.3)
  call(rt, "imu", "flat")
  with pytest.raises(ValueError, match="didn't see the robot tilt"):
    call(rt, "imu", "nose_up")
