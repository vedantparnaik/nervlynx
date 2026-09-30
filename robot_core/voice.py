"""`voice`: talk to the robot through a microphone on it, and hear it answer.

Speech is recognised on the robot and offline with Vosk, listening through `arecord`
(ALSA, already on Raspberry Pi OS). Phrases that start with the wake word ("robot, turn
left") are published on `agent.command` for the `agent` node; saying just the wake word
makes the next phrase count too. Replies on `agent.say` are spoken with espeak-ng.

  pip install vosk && sudo apt install espeak-ng
  curl -LO https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
  unzip vosk-model-small-en-us-0.15.zip     # about 40 MB; then model: ~/vosk-model-small-en-us-0.15

The wake word keeps the robot from acting on its own replies or on background chatter.
Off the robot (the mock backend) the node listens to nothing, so configs still run in
the simulator.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable

from robot_core.hardware import HardwareUnavailable, resolve_backend
from robot_core.live import LiveNode, NodeContext, Output
from robot_core.runtime import RuntimeMessage

_WAKE_WINDOW_NS = 6_000_000_000


class Voice(LiveNode):
  """Microphone to `agent.command`, and `agent.say` to the speaker."""

  rate_hz = 10.0

  def __init__(
    self,
    *,
    model: str = "~/vosk-model-small-en-us-0.15",
    wake_word: str | None = "robot",
    device: str = "default",
    sample_rate: int = 16000,
    command_topic: str = "agent.command",
    say_topic: str = "agent.say",
    speak: bool = True,
    voice: str = "en",
    words_per_minute: int = 160,
    backend: str = "mock",
    popen: Callable[..., Any] | None = None,
  ) -> None:
    if sample_rate not in (8000, 16000, 44100, 48000):
      raise ValueError("sample_rate must be 8000, 16000, 44100, or 48000")
    if not 80 <= words_per_minute <= 400:
      raise ValueError("words_per_minute must be 80..400")
    self.model_path = Path(model).expanduser()
    self.wake_word = (wake_word or "").strip().lower() or None
    self.device = device
    self.sample_rate = int(sample_rate)
    self.command_topic = command_topic
    self.say_topic = say_topic
    self.input_topics = (say_topic,)
    self.speak = bool(speak)
    self.voice = voice
    self.words_per_minute = int(words_per_minute)
    self.backend_name = backend
    self._popen = popen or subprocess.Popen
    self._recorder: Any = None
    self._speaker: Any = None
    self._thread: threading.Thread | None = None
    self._heard: deque[str] = deque(maxlen=16)
    self._lock = threading.Lock()
    self._awake_until = 0
    self._now = 0
    self._error: str | None = None
    self.active = False
    self.heard = 0
    self.ignored = 0
    self.spoken = 0

  def setup(self, ctx: NodeContext) -> None:
    self._now = ctx.now_ns
    if resolve_backend(self.backend_name) == "mock":
      return
    try:
      from vosk import KaldiRecognizer, Model, SetLogLevel  # type: ignore[import-not-found]
    except ImportError as exc:
      raise HardwareUnavailable("the voice node needs Vosk: pip install vosk") from exc
    if not self.model_path.is_dir():
      raise HardwareUnavailable(
        f"no Vosk model at {self.model_path}: download one from https://alphacephei.com/vosk/models "
        "(vosk-model-small-en-us-0.15 is about 40 MB), unzip it, and set model: to its folder"
      )
    if shutil.which("arecord") is None and self._popen is subprocess.Popen:
      raise HardwareUnavailable("arecord is missing: sudo apt install alsa-utils")
    SetLogLevel(-1)
    recognizer = KaldiRecognizer(Model(str(self.model_path)), self.sample_rate)
    command = ["arecord", "-q", "-D", self.device, "-f", "S16_LE", "-r", str(self.sample_rate), "-c", "1", "-t", "raw"]
    self._recorder = self._popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    self.active = True

    def listen() -> None:
      stream = self._recorder.stdout
      while True:
        chunk = stream.read(4000)
        if not chunk:
          self._error = "the microphone stopped (is it plugged in? `arecord -l` lists them)"
          return
        if recognizer.AcceptWaveform(chunk):
          text = json.loads(recognizer.Result()).get("text", "").strip()
          if text:
            with self._lock:
              self._heard.append(text)

    self._thread = threading.Thread(target=listen, name="nervlynx-voice", daemon=True)
    self._thread.start()

  def _command(self, text: str) -> str | None:
    """What to send for a recognised phrase, or None to ignore it."""
    words = re.sub(r"[^\w\s'-]", " ", text.lower()).split()
    if self.wake_word is None:
      return " ".join(words) or None
    wake = self.wake_word.split()
    if words[: len(wake)] == wake:
      rest = words[len(wake) :]
      if not rest:
        self._awake_until = self._now + _WAKE_WINDOW_NS  # "robot" ... "turn left"
        return None
      return " ".join(rest)
    if self._now < self._awake_until:
      self._awake_until = 0
      return " ".join(words) or None
    return None

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    self._now = ctx.now_ns
    if self._error is not None:
      error, self._error = self._error, None
      ctx.fault(f"voice: {error}", kind="voice", severity="error")
    with self._lock:
      heard = list(self._heard)
      self._heard.clear()
    out: list[Output] = []
    for text in heard:
      self.heard += 1
      command = self._command(text)
      if command is None:
        self.ignored += 1
        continue
      out.append((self.command_topic, "Command", {"text": command, "source": "voice"}))
    return out or None

  def on_message(self, msg: RuntimeMessage, ctx: NodeContext) -> Iterable[Output] | None:
    text = str(msg.payload.get("text", "")).strip()
    if not text or not self.speak or not self.active:
      return None
    if self._popen is subprocess.Popen and shutil.which("espeak-ng") is None:
      ctx.fault("voice: espeak-ng is missing, so replies are not spoken (sudo apt install espeak-ng)", kind="voice")
      return None
    if self._speaker is not None and self._speaker.poll() is None:
      self._speaker.terminate()  # the newest reply wins
    self._speaker = self._popen(["espeak-ng", "-v", self.voice, "-s", str(self.words_per_minute), text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    self.spoken += 1
    return None

  def teardown(self, ctx: NodeContext) -> None:
    for process in (self._recorder, self._speaker):
      if process is not None and process.poll() is None:
        process.terminate()
    if self._thread is not None:
      self._thread.join(timeout=1.0)

  def status(self) -> dict[str, Any]:
    return {
      "listening": self.active,
      "wake_word": self.wake_word,
      "heard": self.heard,
      "ignored": self.ignored,
      "spoken": self.spoken,
    }
