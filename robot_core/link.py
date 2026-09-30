"""`esp32_link`: drive motors and read encoders through a microcontroller over USB serial.

NervLynx Link v1 is newline-delimited JSON at 115200 baud, readable in any serial monitor:

  host -> board   {"t": "hello"}                       on connect
                  {"t": "drive", "l": -1..1, "r": -1..1}  every tick (zeros when idle)
  board -> host   {"t": "hello", "fw": "nervlynx-link", "v": 1, "board": "esp32"}
                  {"t": "enc", "l": ticks, "r": ticks}   cumulative encoder counts
                  {"t": "wd"}                             the board stopped the motors: no drive
                                                          command for its watchdog window
                  {"t": "err", "msg": "..."}

The board stops the motors itself if drive commands stop (e.g. the Pi crashed or the cable
came out), independent of everything on the Pi. Reads are non-blocking (only the bytes
already received), so a half-sent line never stalls the executor.
"""

from __future__ import annotations

import json
import math
import threading
from typing import Any, Callable, Iterable

from robot_core.drive import mix_arcade
from robot_core.hardware import HardwareUnavailable, resolve_backend
from robot_core.live import LiveNode, NodeContext, Output
from robot_core.runtime import RuntimeMessage

PROTOCOL_VERSION = 1
_MAX_LINE = 512
_LINK_USB_IDS = ("10c4:ea60", "1a86:7523", "1a86:55d4", "303a:1001", "2e8a:0005", "2e8a:000a")


class SimulatedLinkBoard:
  """The Link firmware's behaviour in Python, for laptops and tests.

  Speaks the protocol through the same `write` / `in_waiting` / `read` calls as pyserial.
  Each wheel turns `ticks_per_command` encoder ticks per drive command at full speed.
  """

  def __init__(self, *, watchdog_commands: int = 15, ticks_per_command: float = 20.0) -> None:
    self._rx = bytearray()
    self._tx = bytearray()
    self.left = self.right = 0.0
    self.ticks = [0.0, 0.0]
    self._idle = 0
    self.watchdog_commands = watchdog_commands
    self.ticks_per_command = ticks_per_command
    self.tripped = False
    self.closed = False

  @property
  def in_waiting(self) -> int:
    return len(self._tx)

  def read(self, size: int = 1) -> bytes:
    data, self._tx = bytes(self._tx[:size]), self._tx[size:]
    return data

  def _send(self, obj: dict[str, Any]) -> None:
    self._tx += (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")

  def write(self, data: bytes) -> int:
    self._rx += data
    while b"\n" in self._rx:
      line, _, rest = bytes(self._rx).partition(b"\n")
      self._rx = bytearray(rest)
      msg = json.loads(line)
      if msg.get("t") == "hello":
        self._send({"t": "hello", "fw": "nervlynx-link-sim", "v": PROTOCOL_VERSION, "board": "sim"})
      elif msg.get("t") == "drive":
        self.left, self.right = float(msg["l"]), float(msg["r"])
        self.tripped = False
        self.ticks[0] += self.left * self.ticks_per_command
        self.ticks[1] += self.right * self.ticks_per_command
        self._send({"t": "enc", "l": int(self.ticks[0]), "r": int(self.ticks[1])})
    return len(data)

  def stall(self, commands: int) -> None:
    """Pretend `commands` drive commands went missing, as when the host hangs."""
    self._idle += commands
    if self._idle >= self.watchdog_commands and not self.tripped:
      self.left = self.right = 0.0
      self.tripped = True
      self._send({"t": "wd"})

  def close(self) -> None:
    self.closed = True


def find_link_port(ports: Iterable[tuple[str, str | None]]) -> str:
  """Pick the one USB serial device that looks like a Link board, or explain the choice."""
  candidates = [path for path, usb_id in ports if usb_id in _LINK_USB_IDS]
  if len(candidates) == 1:
    return candidates[0]
  if not candidates:
    raise HardwareUnavailable("no ESP32/Pico found on USB serial; plug it in or set port: /dev/ttyUSB0 (see `nervlynx scan`)")
  raise HardwareUnavailable(f"several boards could be the link ({', '.join(candidates)}); set port: to the right one")


def _open_serial(port: str, baud: int) -> Any:
  try:
    import serial  # type: ignore[import-not-found]
  except ImportError as exc:
    raise HardwareUnavailable("esp32_link needs pyserial: pip install pyserial (or sudo apt install python3-serial)") from exc
  return serial.Serial(port, baud, timeout=0, write_timeout=0)


def _detected_ports() -> list[tuple[str, str | None]]:
  from robot_core.doctor import System
  from robot_core.scan import _usb_id

  system = System()
  return [(path, _usb_id(system, path.rsplit("/", 1)[-1])) for path in system.glob("/dev/ttyUSB*") + system.glob("/dev/ttyACM*")]


class Esp32Link(LiveNode):
  """Sends `cmd.drive` to a Link board every tick and publishes its encoder counts."""

  rate_hz = 50.0

  def __init__(
    self,
    *,
    port: str = "auto",
    baud: int = 115200,
    command_topic: str = "cmd.drive",
    encoder_topic: str = "link.encoders",
    deadman_s: float = 0.25,
    max_speed: float = 1.0,
    link_timeout_s: float = 1.0,
    backend: str = "mock",
    open_serial: Callable[[str, int], Any] | None = None,
  ) -> None:
    if deadman_s <= 0 or link_timeout_s <= 0:
      raise ValueError("deadman_s and link_timeout_s must be > 0")
    if not 0 < max_speed <= 1:
      raise ValueError("max_speed must be in (0, 1]")
    self.port_name = port
    self.baud = int(baud)
    self.command_topic = command_topic
    self.encoder_topic = encoder_topic
    self.input_topics = (command_topic,)
    self.deadman_ns = int(deadman_s * 1e9)
    self.max_speed = float(max_speed)
    self.link_timeout_ns = int(link_timeout_s * 1e9)
    self.backend_name = backend
    self._open_serial = open_serial or _open_serial
    self._port: Any = None
    self._lock = threading.Lock()
    self._buffer = bytearray()
    self._target = (0.0, 0.0)
    self._last_cmd_ns: int | None = None
    self._last_rx_ns: int | None = None
    self._connected = False
    self._lost_reported = False
    self.firmware: dict[str, Any] | None = None
    self.encoders: dict[str, Any] | None = None
    self.watchdog_trips = 0
    self.bad_lines = 0
    self.tx = self.rx = 0

  def setup(self, ctx: NodeContext) -> None:
    resolved = resolve_backend(self.backend_name)
    if resolved == "mock":
      self._port = SimulatedLinkBoard()
      self.port_name = "simulated"
    else:
      name = self.port_name if self.port_name != "auto" else find_link_port(_detected_ports())
      self._port = self._open_serial(name, self.baud)
      self.port_name = name
    self._last_rx_ns = ctx.now_ns
    self._write({"t": "hello"})

  def _write(self, obj: dict[str, Any]) -> None:
    line = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
    with self._lock:
      if self._port is not None:
        self._port.write(line)
        self.tx += 1

  def _read_lines(self) -> list[dict[str, Any]]:
    waiting = self._port.in_waiting
    if waiting:
      self._buffer += self._port.read(waiting)
    messages = []
    while b"\n" in self._buffer:
      raw, _, rest = bytes(self._buffer).partition(b"\n")
      self._buffer = bytearray(rest)
      try:
        msg = json.loads(raw)
      except ValueError:
        self.bad_lines += 1
        continue
      if isinstance(msg, dict):
        messages.append(msg)
    if len(self._buffer) > _MAX_LINE:
      self.bad_lines += 1
      self._buffer.clear()
    return messages

  def on_message(self, msg: RuntimeMessage, ctx: NodeContext) -> Iterable[Output] | None:
    p = msg.payload
    try:
      if "left" in p and "right" in p:
        left, right = float(p["left"]), float(p["right"])
      else:
        left, right = mix_arcade(float(p.get("linear", 0.0)), float(p.get("angular", 0.0)))
    except (TypeError, ValueError):
      ctx.fault(f"esp32_link rejected drive command {p!r}", kind="bad_command")
      return None
    if math.isfinite(left) and math.isfinite(right):
      self._target = (max(-1.0, min(1.0, left)), max(-1.0, min(1.0, right)))
      self._last_cmd_ns = ctx.now_ns
    return None

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    now = ctx.now_ns
    out: list[Output] = []
    for msg in self._read_lines():
      self.rx += 1
      self._last_rx_ns = now
      kind = msg.get("t")
      if kind == "hello":
        self.firmware = {k: msg.get(k) for k in ("fw", "v", "board")}
        if msg.get("v") != PROTOCOL_VERSION:
          ctx.fault(f"link firmware speaks protocol v{msg.get('v')}, NervLynx expects v{PROTOCOL_VERSION}; reflash the board", kind="link")
      elif kind == "enc":
        self.encoders = {"left_ticks": int(msg.get("l", 0)), "right_ticks": int(msg.get("r", 0))}
        out.append((self.encoder_topic, "EncoderTicks", dict(self.encoders)))
      elif kind == "wd":
        self.watchdog_trips += 1
        ctx.fault("link board stopped the motors: its watchdog saw no drive commands", kind="link")
      elif kind == "err":
        ctx.fault(f"link board error: {msg.get('msg')}", kind="link")
    silent = now - (self._last_rx_ns if self._last_rx_ns is not None else now)
    self._connected = silent <= self.link_timeout_ns
    if not self._connected and not self._lost_reported:
      self._lost_reported = True
      ctx.fault(f"link: no data from the board on {self.port_name} for {silent / 1e9:.1f} s (check the USB cable and firmware)", kind="link", severity="error")
    elif self._connected:
      self._lost_reported = False
    fresh = self._last_cmd_ns is not None and now - self._last_cmd_ns <= self.deadman_ns
    left, right = self._target if fresh and not ctx.estop_engaged else (0.0, 0.0)
    self._write({"t": "drive", "l": round(left * self.max_speed, 4), "r": round(right * self.max_speed, 4)})
    return out or None

  def safe_stop(self, ctx: NodeContext) -> None:
    self._target = (0.0, 0.0)
    self._last_cmd_ns = None
    self._write({"t": "drive", "l": 0.0, "r": 0.0})

  def hard_stop(self) -> None:
    self._write({"t": "drive", "l": 0.0, "r": 0.0})

  def teardown(self, ctx: NodeContext) -> None:
    self._write({"t": "drive", "l": 0.0, "r": 0.0})
    with self._lock:
      if self._port is not None:
        self._port.close()
        self._port = None

  def status(self) -> dict[str, Any]:
    return {
      "port": self.port_name,
      "connected": self._connected,
      "firmware": self.firmware,
      "encoders": self.encoders,
      "watchdog_trips": self.watchdog_trips,
      "tx": self.tx,
      "rx": self.rx,
      "bad_lines": self.bad_lines,
    }
