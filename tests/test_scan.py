import yaml
from typer.testing import CliRunner

from robot_core.nervlynx_cli import app
from robot_core.scan import draft_nodes_yaml, scan, scan_i2c
from test_doctor import FakeSystem, PI5_MODEL


class FakeI2C:
  def __init__(self, devices: dict[int, dict[int, int]]):
    self.devices = devices
    self.closed = False

  def _check(self, address: int) -> None:
    if address not in self.devices:
      raise OSError(121, "Remote I/O error")

  def read_byte(self, address: int) -> int:
    self._check(address)
    return 0

  def write_quick(self, address: int) -> None:
    self._check(address)

  def read_byte_data(self, address: int, register: int) -> int:
    self._check(address)
    return self.devices[address].get(register, 0x00)

  def close(self) -> None:
    self.closed = True


BENCH = {
  0x68: {0x75: 0x68},
  0x69: {0x75: 0x00, 0x00: 0xEA},
  0x29: {0xC0: 0xEE},
  0x76: {0xD0: 0x60},
  0x40: {0x00: 0x11},
  0x3C: {},
  0x50: {},
}


def test_i2c_devices_are_identified_by_chip_id_not_address() -> None:
  found = {item.where: item for item in scan_i2c(FakeI2C(BENCH))}
  assert found["0x68"].name == "MPU6050 IMU" and found["0x68"].node == "mpu6050_imu"
  assert found["0x68"].params == {"address": 0x68}
  assert found["0x69"].name == "ICM-20948 IMU" and found["0x69"].node is None
  assert found["0x29"].name == "VL53L0X time-of-flight distance sensor"
  assert found["0x76"].name.startswith("BME280")
  assert found["0x40"].name.startswith("PCA9685")
  assert found["0x3c"].name.startswith("SSD1306")
  assert found["0x50"].name == "unknown device"
  assert set(found) == {"0x68", "0x69", "0x29", "0x76", "0x40", "0x3c", "0x50"}


def pi_with_usb(**overrides) -> FakeSystem:
  files = {
    "/proc/device-tree/model": PI5_MODEL,
    "/sys/class/tty/ttyUSB0/idVendor": "10c4\n",
    "/sys/class/tty/ttyUSB0/idProduct": "ea60\n",
    "/sys/class/tty/ttyACM0/idVendor": "303a\n",
    "/sys/class/tty/ttyACM0/idProduct": "1001\n",
  }
  params = dict(
    files=files,
    devices=("/dev/i2c-1", "/dev/ttyUSB0", "/dev/ttyACM0"),
    commands={"rpicam-hello": "Available cameras\n0 : imx708 [4608x2592]\n"},
  )
  params.update(overrides)
  return FakeSystem(**params)


def test_scan_reports_usb_serial_cameras_and_closes_the_bus() -> None:
  bus = FakeI2C({0x68: {0x75: 0x68}})
  found, notes = scan(pi_with_usb(), open_bus=lambda n: bus)
  by_where = {item.where: item for item in found}
  assert notes == [] and bus.closed
  assert by_where["/dev/ttyUSB0"].name.startswith("CP210x USB-serial") and by_where["/dev/ttyUSB0"].note.startswith("USB id 10c4:ea60; a LiDAR is")
  assert by_where["/dev/ttyACM0"].name == "ESP32 with native USB (S2/S3/C3)"
  assert by_where["imx708"].node == "camera"


def test_servo_boards_and_gps_receivers_get_nodes_that_validate() -> None:
  from robot_core.live_config import validate_live_config
  from robot_core.project import build_registry

  files = {"/proc/device-tree/model": PI5_MODEL, "/sys/class/tty/ttyACM0/idVendor": "1546\n", "/sys/class/tty/ttyACM0/idProduct": "01a8\n"}
  system = FakeSystem(files=files, devices=("/dev/i2c-1", "/dev/ttyACM0"), commands={})
  found, _ = scan(system, open_bus=lambda n: FakeI2C({0x40: {0x00: 0x11}}))
  nodes = yaml.safe_load(draft_nodes_yaml(found))
  assert nodes == [
    {"plugin": "pca9685_servos", "params": {"address": 0x40, "servos": [{"name": "servo0", "channel": 0}]}},
    {"plugin": "gps_nmea", "params": {"port": "/dev/ttyACM0"}},
  ]
  assert validate_live_config({"nodes": nodes}, build_registry()) == []


def test_scan_explains_why_i2c_was_skipped() -> None:
  _, off = scan(pi_with_usb(devices=("/dev/ttyUSB0",)), open_bus=lambda n: FakeI2C({}))
  assert off == ["I2C bus 1 is off, so I2C sensors were not scanned: sudo raspi-config nonint do_i2c 0, then reboot"]

  def no_smbus(n):
    raise ImportError("smbus2")

  _, missing = scan(pi_with_usb(), open_bus=no_smbus)
  assert missing == ["install smbus2 to scan I2C: pip install smbus2"]


def test_draft_yaml_is_valid_and_ready_to_paste() -> None:
  found, _ = scan(pi_with_usb(), open_bus=lambda n: FakeI2C({0x68: {0x75: 0x70}, 0x76: {0xD0: 0x58}}))
  draft = draft_nodes_yaml(found)
  nodes = yaml.safe_load(draft)
  assert nodes == [
    {"plugin": "mpu6050_imu", "params": {"address": 0x68}},
    {"plugin": "camera", "params": {"name": "front", "optional": True}},
  ]
  assert "# BMP280 temperature/pressure sensor at 0x76 (no built-in node yet)" in draft
  assert "MPU6500 IMU at 0x68" in draft


def test_scan_cli_runs_on_this_machine(tmp_path) -> None:
  out = tmp_path / "found.yaml"
  result = CliRunner().invoke(app, ["scan", "--output", str(out)])
  assert result.exit_code == 0, result.stdout
  assert out.read_text(encoding="utf-8").startswith("# Found by `nervlynx scan`")
