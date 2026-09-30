import io
import sys
import time
import types

import pytest

from robot_core.hardware import HardwareUnavailable
from robot_core.live import LiveRuntime
from robot_core.runtime import SimulatedClock
from robot_core.voice import Voice


class FakeProcess:
  def __init__(self, argv, stdout=None, **kwargs):
    self.argv = argv
    self.stdout = io.BytesIO(b"\0" * 4000 * FakeProcess.chunks) if argv[0] == "arecord" else None
    self.running = True

  def poll(self):
    return None if self.running else 0

  def terminate(self):
    self.running = False


def install_vosk(monkeypatch, phrases: list[str]) -> None:
  heard = list(phrases)

  class Recognizer:
    def __init__(self, model, rate):
      self.rate = rate

    def AcceptWaveform(self, chunk):  # noqa: N802 - Vosk's name
      return bool(heard)

    def Result(self):  # noqa: N802 - Vosk's name
      return '{"text": "%s"}' % heard.pop(0)

  monkeypatch.setitem(sys.modules, "vosk", types.SimpleNamespace(Model=lambda path: path, KaldiRecognizer=Recognizer, SetLogLevel=lambda level: None))
  FakeProcess.chunks = len(phrases)


def voice_runtime(tmp_path, monkeypatch, phrases, **params):
  install_vosk(monkeypatch, phrases)
  started: list = []

  def popen(argv, **kwargs):
    started.append(FakeProcess(argv, **kwargs))
    return started[-1]

  node = Voice(model=str(tmp_path), backend="gpiozero", popen=popen, **params)
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("voice", node)
  commands: list = []
  rt.subscribe("probe", "agent.command", commands.append)
  rt.start()
  deadline = time.monotonic() + 3.0
  while node._thread.is_alive() and time.monotonic() < deadline:
    time.sleep(0.01)
  for _ in range(5):
    rt.clock.advance_ms(100)
    rt.step()
  return rt, node, commands, started


def test_only_phrases_with_the_wake_word_become_commands(tmp_path, monkeypatch) -> None:
  rt, node, commands, started = voice_runtime(tmp_path, monkeypatch, ["robot turn left", "hello there", "robot", "drive forward one metre", "Robot, stop!"])
  assert [m.payload for m in commands] == [
    {"text": "turn left", "source": "voice"},
    {"text": "drive forward one metre", "source": "voice"},
    {"text": "stop", "source": "voice"},
  ]
  assert node.status()["ignored"] == 2
  assert started[0].argv[:3] == ["arecord", "-q", "-D"] and "16000" in started[0].argv
  assert any(e.message.startswith("voice: the microphone stopped") for e in rt.fault_events)


def test_replies_are_spoken_and_the_newest_wins(tmp_path, monkeypatch) -> None:
  rt, node, _, started = voice_runtime(tmp_path, monkeypatch, [])
  rt.publish_external("agent.say", "Speech", {"text": "Turning left 90 degrees.", "id": 1})
  rt.clock.advance_ms(100)
  rt.step()
  rt.publish_external("agent.say", "Speech", {"text": "Stopping.", "id": 2})
  rt.clock.advance_ms(100)
  rt.step()
  speakers = [p for p in started if p.argv[0] == "espeak-ng"]
  assert [p.argv[-1] for p in speakers] == ["Turning left 90 degrees.", "Stopping."]
  assert speakers[0].running is False and node.status()["spoken"] == 2
  rt.shutdown()
  assert all(not p.running for p in started)


def test_no_wake_word_forwards_everything(tmp_path, monkeypatch) -> None:
  _, _, commands, _ = voice_runtime(tmp_path, monkeypatch, ["turn right"], wake_word=None)
  assert [m.payload["text"] for m in commands] == ["turn right"]


def test_voice_explains_what_is_missing(tmp_path, monkeypatch) -> None:
  install_vosk(monkeypatch, [])
  with pytest.raises(HardwareUnavailable, match="no Vosk model at"):
    Voice(model=str(tmp_path / "missing"), backend="gpiozero").setup(types.SimpleNamespace(now_ns=0))
  monkeypatch.setitem(sys.modules, "vosk", None)
  with pytest.raises(HardwareUnavailable, match="pip install vosk"):
    Voice(backend="gpiozero").setup(types.SimpleNamespace(now_ns=0))
  mock = Voice()
  mock.setup(types.SimpleNamespace(now_ns=0))
  assert mock.status()["listening"] is False
