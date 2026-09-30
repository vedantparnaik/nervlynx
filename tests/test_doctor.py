import fnmatch
import json
from pathlib import Path

from typer.testing import CliRunner

from robot_core.doctor import FAIL, INFO, OK, WARN, System, render_text, run_checks
from robot_core.nervlynx_cli import app

PI5_MODEL = "Raspberry Pi 5 Model B Rev 1.0\x00"
MEMINFO = "MemTotal:        8245000 kB\nMemFree:  100 kB\nMemAvailable:    6000000 kB\n"


class FakeSystem(System):
  def __init__(
    self,
    *,
    files: dict[str, str] | None = None,
    devices: tuple[str, ...] = (),
    locked: tuple[str, ...] = (),
    commands: dict[str, str] | None = None,
    modules: tuple[str, ...] = (),
    groups: tuple[str, ...] = ("gpio", "i2c", "dialout", "video"),
    venv: tuple[bool, bool] = (True, True),
    python: tuple[int, int, int] = (3, 11, 2),
    disk_free: int = 20 * 2**30,
    platform: str = "Linux aarch64",
  ) -> None:
    self.files = files or {}
    self.devices = devices
    self.locked = locked
    self.commands = commands or {}
    self.modules = modules
    self._groups = set(groups)
    self._venv = venv
    self._python = python
    self._disk_free = disk_free
    self._platform = platform

  def read_text(self, path: str) -> str | None:
    return self.files.get(path)

  def exists(self, path: str) -> bool:
    return path in self.devices

  def accessible(self, path: str) -> bool:
    return path in self.devices and path not in self.locked

  def glob(self, pattern: str) -> list[str]:
    return sorted(dev for dev in self.devices if fnmatch.fnmatch(dev, pattern))

  def run(self, argv: list[str], timeout_s: float = 5.0) -> str | None:
    return self.commands.get(argv[0])

  def importable(self, module: str) -> bool:
    return module in self.modules

  def groups(self) -> set[str]:
    return self._groups

  def python_version(self) -> tuple[int, int, int]:
    return self._python

  def venv(self) -> tuple[bool, bool]:
    return self._venv

  def disk_free_bytes(self, path: str) -> int | None:
    return self._disk_free

  def hostname(self) -> str:
    return "rover"

  def platform_label(self) -> str:
    return self._platform


def healthy_pi5(**overrides) -> FakeSystem:
  params = dict(
    files={
      "/proc/device-tree/model": PI5_MODEL,
      "/sys/class/thermal/thermal_zone0/temp": "51234\n",
      "/proc/meminfo": MEMINFO,
    },
    devices=("/dev/gpiochip0", "/dev/gpiochip4", "/dev/i2c-1", "/dev/spidev0.0"),
    commands={
      "vcgencmd": "throttled=0x0\n",
      "rpicam-hello": "Available cameras\n-----------------\n0 : imx708 [4608x2592 10-bit RGGB] (/base/axi/rp1)\n",
    },
    modules=("gpiozero", "lgpio", "picamera2", "smbus2"),
  )
  params.update(overrides)
  return FakeSystem(**params)


def by_name(checks) -> dict:
  return {check.name: check for check in checks}


def test_healthy_pi5_reports_all_good() -> None:
  checks = run_checks(healthy_pi5())
  statuses = {check.name: check.status for check in checks}
  assert FAIL not in statuses.values() and WARN not in statuses.values()
  assert by_name(checks)["gpio"].summary == "backend auto -> gpiozero, pins accessible"
  assert by_name(checks)["camera"].summary == "Pi camera: imx708"
  assert by_name(checks)["temperature"].summary == "CPU at 51.2 °C"
  assert "http://rover.local:9120" in by_name(checks)["dashboard"].summary
  assert render_text(checks).endswith("all good")


def test_pi5_with_only_rpi_gpio_explains_the_lgpio_fix() -> None:
  gpio = by_name(run_checks(healthy_pi5(modules=("RPi.GPIO", "smbus2", "picamera2"))))["gpio"]
  assert gpio.status == FAIL
  assert "RPi.GPIO does not support it" in gpio.summary
  assert "python3-lgpio" in gpio.fix


def test_gpio_permission_problem_points_at_the_gpio_group() -> None:
  system = healthy_pi5(locked=("/dev/gpiochip0", "/dev/gpiochip4"))
  gpio = by_name(run_checks(system))["gpio"]
  assert gpio.status == FAIL
  assert "usermod -aG gpio" in gpio.fix


def test_power_distinguishes_live_and_past_under_voltage() -> None:
  now = by_name(run_checks(healthy_pi5(commands={"vcgencmd": "throttled=0x50005"})))["power"]
  assert now.status == FAIL and "right now" in now.summary and "own battery" in now.fix
  past = by_name(run_checks(healthy_pi5(commands={"vcgencmd": "throttled=0x50000"})))["power"]
  assert past.status == WARN and "since boot" in past.summary
  hot = by_name(run_checks(healthy_pi5(commands={"vcgencmd": "throttled=0x80000"})))["power"]
  assert hot.status == WARN and "throttled since boot" in hot.summary


def test_i2c_off_gives_the_raspi_config_command() -> None:
  system = healthy_pi5(devices=("/dev/gpiochip4",))
  i2c = by_name(run_checks(system))["i2c"]
  assert i2c.status == WARN
  assert i2c.fix == "sudo raspi-config nonint do_i2c 0, then reboot."


def test_hot_cpu_and_low_memory_warn() -> None:
  files = {
    "/proc/device-tree/model": PI5_MODEL,
    "/sys/class/thermal/thermal_zone0/temp": "82000",
    "/proc/meminfo": "MemTotal: 427000 kB\nMemAvailable: 90000 kB\n",
  }
  checks = by_name(run_checks(healthy_pi5(files=files)))
  assert checks["temperature"].status == FAIL
  assert checks["memory"].status == WARN and "87 MB of 416 MB" in checks["memory"].summary


def test_venv_without_system_packages_is_flagged_on_a_pi() -> None:
  python = by_name(run_checks(healthy_pi5(venv=(True, False))))["python"]
  assert python.status == WARN
  assert "--system-site-packages" in python.fix


def test_camera_found_without_its_library() -> None:
  camera = by_name(run_checks(healthy_pi5(modules=("gpiozero", "lgpio", "smbus2"))))["camera"]
  assert camera.status == WARN and "python3-picamera2" in camera.fix
  usb = healthy_pi5(commands={"vcgencmd": "throttled=0x0", "rpicam-hello": "No cameras available!"}, devices=(
    "/dev/gpiochip4", "/dev/i2c-1", "/dev/v4l/by-id/usb-046d_C920_ABC-video-index0",
  ))
  assert by_name(run_checks(usb))["camera"].summary == "USB camera found (usb-046d_C920_ABC-video-index0), but OpenCV isn't installed"


def test_serial_devices_need_the_dialout_group() -> None:
  system = healthy_pi5(
    devices=("/dev/gpiochip4", "/dev/i2c-1", "/dev/ttyUSB0", "/dev/serial/by-id/usb-Silicon_Labs_CP2102-if00-port0"),
    groups=("gpio", "i2c"),
  )
  serial = by_name(run_checks(system))["serial"]
  assert serial.status == WARN and "usb-Silicon_Labs_CP2102-if00-port0" in serial.summary
  assert "dialout" in serial.fix


def test_laptop_skips_pi_only_checks() -> None:
  laptop = FakeSystem(platform="Darwin arm64", venv=(True, False))
  checks = by_name(run_checks(laptop))
  assert checks["board"].status == INFO and "not a Raspberry Pi" in checks["board"].summary
  assert checks["gpio"].summary == "backend auto -> mock (no GPIO on this machine)"
  assert checks["python"].status == OK
  for pi_only in ("i2c", "spi", "power", "temperature", "dashboard", "camera", "serial"):
    assert pi_only not in checks


def test_config_check_uses_live_validation(tmp_path: Path) -> None:
  laptop = FakeSystem(platform="Darwin arm64")
  good = by_name(run_checks(laptop, config=Path("examples/live/rover_sim.yaml")))["config"]
  assert good.status == OK and "(3 nodes)" in good.summary
  broken = tmp_path / "robot.yaml"
  broken.write_text("nodes: []\n", encoding="utf-8")
  bad = by_name(run_checks(laptop, config=broken))["config"]
  assert bad.status == FAIL and "nodes must be a non-empty list" in bad.summary


def test_cli_doctor_json_runs_on_this_machine() -> None:
  result = CliRunner().invoke(app, ["doctor", "--json"])
  assert result.exit_code == 0, result.stdout
  names = {check["name"] for check in json.loads(result.stdout)}
  assert {"python", "board", "gpio", "packages"} <= names
