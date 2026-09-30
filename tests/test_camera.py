import struct
import sys
import threading
import time
import types
import zlib
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from robot_core.camera import CameraNode, FrameBuffer, color_bars, frames
from robot_core.live import LiveRuntime
from robot_core.runtime import SimulatedClock
from robot_core.server import serve_live


def camera_messages(node: CameraNode, seconds: float = 1.0) -> list[dict]:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("camera", node)
  out: list[dict] = []
  rt.add_message_listener(lambda msg: out.append(msg.payload) if msg.envelope.topic == node.topic else None)
  rt.run(duration_s=seconds)
  return out


def test_color_bars_are_a_valid_png() -> None:
  png = color_bars(64, 48, seq=3)
  assert png.startswith(b"\x89PNG\r\n\x1a\n")
  width, height = struct.unpack(">II", png[16:24])
  assert (width, height) == (64, 48)
  idat_len = struct.unpack(">I", png[33:37])[0]
  assert png[37:41] == b"IDAT"
  assert len(zlib.decompress(png[41 : 41 + idat_len])) == 48 * (1 + 3 * 64)
  assert color_bars(64, 48, seq=4) != png


def test_frame_buffer_hands_out_only_newer_frames() -> None:
  buffer = FrameBuffer()
  assert buffer.latest() is None and buffer.wait_newer(0, 0.01) is None
  first = buffer.put(b"a", "image/png", 1, 1)
  assert buffer.wait_newer(0, 0.01) == first and buffer.wait_newer(first.seq, 0.01) is None
  threading.Timer(0.05, lambda: buffer.put(b"b", "image/png", 1, 1)).start()
  assert buffer.wait_newer(first.seq, 2.0).data == b"b"


def test_mock_camera_publishes_small_deterministic_metadata() -> None:
  first = camera_messages(CameraNode(name="det", fps=10, width=160, height=120))
  second = camera_messages(CameraNode(name="det", fps=10, width=160, height=120))
  assert first == second and len(first) == 11
  assert first[-1]["seq"] == 11 and first[-1]["format"] == "png" and first[-1]["fps"] == 10.0
  assert (first[-1]["width"], first[-1]["height"]) == (160, 120)


def test_frames_registry_follows_the_camera_lifecycle() -> None:
  node = CameraNode(name="lifecycle", fps=5)
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("camera", node)
  rt.start()
  rt.step()
  assert frames("lifecycle") is node.frame_buffer and node.frame_buffer.latest() is not None
  rt.shutdown()
  assert frames("lifecycle") is None


def test_opencv_source_captures_on_its_own_thread(monkeypatch) -> None:
  class FakeCapture:
    def __init__(self, device):
      self.device, self.released = device, False

    def isOpened(self):
      return True

    def set(self, prop, value):
      return True

    def read(self):
      time.sleep(0.005)
      return True, types.SimpleNamespace(shape=(120, 160, 3))

    def release(self):
      self.released = True

  captures: list = []
  fake_cv2 = types.SimpleNamespace(
    VideoCapture=lambda device: captures.append(FakeCapture(device)) or captures[-1],
    CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4, CAP_PROP_FPS=5, IMWRITE_JPEG_QUALITY=1,
    imencode=lambda ext, image, params: (True, types.SimpleNamespace(tobytes=lambda: b"\xff\xd8jpeg\xff\xd9")),
  )
  monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
  node = CameraNode(name="usb", source="opencv", device=2, fps=20)
  rt = LiveRuntime(seed=1)
  rt.add_node("camera", node)
  seen: list = []
  rt.add_message_listener(lambda msg: seen.append(msg.payload) if msg.envelope.topic == "camera.usb" else None)
  rt.run(duration_s=0.4)
  assert captures[0].device == 2 and captures[0].released
  assert seen and seen[-1]["format"] == "jpeg" and (seen[-1]["width"], seen[-1]["height"]) == (160, 120)


def test_picamera2_source_streams_encoder_output(monkeypatch) -> None:
  state: dict = {}

  class FakePicamera2:
    @staticmethod
    def global_camera_info():
      return [{"Model": "imx708"}]

    def create_video_configuration(self, **kwargs):
      return kwargs

    def configure(self, config):
      state["config"] = config

    def start_recording(self, encoder, output):
      state["quality"] = encoder.q
      output.file.write(b"\xff\xd8frame\xff\xd9")

    def stop_recording(self):
      state["stopped"] = True

    def close(self):
      state["closed"] = True

  monkeypatch.setitem(sys.modules, "picamera2", types.SimpleNamespace(Picamera2=FakePicamera2))
  monkeypatch.setitem(sys.modules, "picamera2.encoders", types.SimpleNamespace(JpegEncoder=lambda q: types.SimpleNamespace(q=q)))
  monkeypatch.setitem(sys.modules, "picamera2.outputs", types.SimpleNamespace(FileOutput=lambda file: types.SimpleNamespace(file=file)))
  node = CameraNode(name="pi", source="picamera2", width=320, height=240, fps=10, quality=70)
  messages = camera_messages(node, seconds=0.2)
  assert state["config"] == {"main": {"size": (320, 240)}, "controls": {"FrameRate": 10.0}}
  assert state["quality"] == 70 and state["stopped"] and state["closed"]
  assert messages[0] == {"seq": 1, "width": 320, "height": 240, "bytes": 9, "fps": 0.0, "format": "jpeg"}


def test_optional_camera_lets_the_robot_run_without_it(monkeypatch) -> None:
  monkeypatch.setitem(sys.modules, "cv2", types.SimpleNamespace(VideoCapture=lambda device: types.SimpleNamespace(isOpened=lambda: False)))
  node = CameraNode(name="gone", source="opencv", optional=True)
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("camera", node)
  rt.run(duration_s=0.5)
  assert node.status()["source"] == "none"
  assert any("camera gone unavailable, continuing without it" in e.message for e in rt.fault_events)
  strict = CameraNode(name="gone", source="opencv")
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("camera", strict)
  with pytest.raises(Exception, match="cannot open camera device 0"):
    rt.start()


def test_bad_camera_params_are_rejected() -> None:
  with pytest.raises(ValueError, match="source"):
    CameraNode(source="webcam")
  with pytest.raises(ValueError, match="fps"):
    CameraNode(fps=0)


def test_dashboard_streams_camera_frames_and_ends_streams_on_shutdown() -> None:
  rt = LiveRuntime(seed=1, name="cam")
  rt.add_node("front_cam", CameraNode(name="front_http", fps=20, width=64, height=48))
  stop = threading.Event()
  runner = threading.Thread(target=rt.run, kwargs={"stop_event": stop}, daemon=True)
  runner.start()
  server = serve_live(rt, host="127.0.0.1", port=0)
  base = f"http://127.0.0.1:{server.server_address[1]}"
  try:
    with urlopen(base + "/camera/front_cam/latest", timeout=3) as resp:
      assert resp.headers["Content-Type"] == "image/png" and resp.read().startswith(b"\x89PNG")
    with urlopen(base + "/camera/front_cam.mjpg", timeout=3) as stream:
      assert stream.headers["Content-Type"] == "multipart/x-mixed-replace; boundary=nervlynxframe"
      assert stream.readline() == b"--nervlynxframe\r\n"
      assert stream.readline() == b"Content-Type: image/png\r\n"
    with pytest.raises(HTTPError) as missing:
      urlopen(base + "/camera/nope.mjpg", timeout=3)
    assert missing.value.code == 404
    with urlopen(base + "/", timeout=3) as page:
      html = page.read().decode("utf-8")
    assert 'id="stick"' in html and "drawWorld" in html and "/camera/" in html
  finally:
    server.shutdown()
    server.server_close()
    stop.set()
    runner.join(timeout=3)
  assert server.stopping.is_set()
