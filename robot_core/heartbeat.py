"""A pin that toggles only while the control loop runs, for hardware that cuts motor power.

Software can stop motors only while it is running. The `heartbeat` node flips a pin on
every tick, so the pin keeps changing only while the executor is stepping. Wire it to a
circuit that enables the motor driver for a short time after each rising edge (a
retriggerable monostable such as a 74HC123 or CD4538, or a small microcontroller), and
the motors lose power by themselves when NervLynx freezes, crashes, is killed, or the
Pi loses power. The pin also stops while the e-stop is latched, from the moment the
stall guard fires, and at shutdown, so all of those cut power in hardware too.

Trigger on edges, never on the level: a pin stuck high is not a heartbeat.
"""

from __future__ import annotations

from typing import Any, Iterable

from robot_core.hardware import BACKENDS, PinBackend, create_backend
from robot_core.live import LiveNode, NodeContext, Output


class Heartbeat(LiveNode):
  """Square wave on `pin` at `frequency_hz` (two ticks per cycle) while the robot may move."""

  def __init__(self, *, pin: int, frequency_hz: float = 25.0, backend: str = "mock") -> None:
    if isinstance(pin, bool) or not isinstance(pin, int) or not 0 <= pin <= 27:
      raise ValueError("pin must be a BCM pin number between 0 and 27")
    if isinstance(frequency_hz, bool) or not isinstance(frequency_hz, (int, float)) or not 1 <= frequency_hz <= 200:
      raise ValueError("frequency_hz must be between 1 and 200")
    if backend not in BACKENDS:
      raise ValueError(f"unknown backend {backend!r}; expected one of {', '.join(BACKENDS)}")
    self.pin = pin
    self.frequency_hz = float(frequency_hz)
    self.rate_hz = 2.0 * self.frequency_hz
    self.backend_name = backend
    self.backend: PinBackend | None = None
    self.level = False
    self.toggling = False
    self.rising_edges = 0
    self._halted = False
    self._saw_estop = False

  def setup(self, ctx: NodeContext) -> None:
    self.backend = create_backend(self.backend_name)
    if self.backend_name == "auto":
      ctx.fault(f"hardware backend auto resolved to {self.backend.name}", severity="info", kind="hardware")
    self.backend.setup_digital(self.pin, False)

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    if ctx.estop_engaged:
      self._saw_estop = True
    elif self._saw_estop:
      # The operator cleared the e-stop; release the hard-stop latch with it.
      self._saw_estop = False
      self._halted = False
    if self._halted or ctx.estop_engaged:
      self.toggling = False
      self._write(False)
      return None
    self.toggling = True
    self._write(not self.level)
    if self.level:
      self.rising_edges += 1
    return None

  def safe_stop(self, ctx: NodeContext) -> None:
    self.toggling = False
    self._write(False)

  def hard_stop(self) -> None:
    # No lock: the executor may be stuck inside tick. If a tick races this and leaves the
    # pin high, the edges still stop, and edges are what the circuit watches.
    self._halted = True
    self.toggling = False
    self._write(False)

  def teardown(self, ctx: NodeContext) -> None:
    self.toggling = False
    self._write(False)
    if self.backend is not None:
      self.backend.close()
      self.backend = None

  def status(self) -> dict[str, Any]:
    return {
      "pin": self.pin,
      "frequency_hz": self.frequency_hz,
      "toggling": self.toggling,
      "rising_edges": self.rising_edges,
      "backend": self.backend.name if self.backend is not None else self.backend_name,
    }

  def gpio_pins(self) -> dict[int, str]:
    return {self.pin: "heartbeat"}

  def _write(self, level: bool) -> None:
    backend = self.backend
    if backend is not None:
      backend.write_digital(self.pin, level)
    self.level = level
