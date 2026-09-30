"""Camera node: frames go to a shared latest-frame buffer, not through the message bus.

Real cameras capture on their own thread (picamera2's JPEG encoder for Pi cameras, OpenCV
for USB webcams) into a `FrameBuffer`. On each tick the node publishes small metadata on
`camera.<name>` ({"seq", "width", "height", "bytes", "fps", "format"}) when a new frame has
arrived, so traces stay small and the executor never waits on a camera. The dashboard
streams the buffer at `/camera/<node>.mjpg`; other nodes read it with `frames(name)`.

The mock source draws a moving test pattern as PNG with only the standard library, inside
`tick`, so simulated-clock runs stay deterministic.
"""

from __future__ import annotations

import io
import struct
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from robot_core.hardware import HardwareUnavailable, _importable, resolve_backend
from robot_core.live import LiveNode, NodeContext, Output

SOURCES = ("auto", "mock", "picamera2", "opencv")
_MOCK_MAX = (320, 240)
_BARS = [(235, 235, 235), (235, 235, 16), (16, 235, 235), (16, 235, 16), (235, 16, 235), (235, 16, 16), (16, 16, 235), (16, 16, 16)]
_STALE_S = 2.0


@dataclass(frozen=True)
class Frame:
  seq: int
  data: bytes
  content_type: str
  width: int
  height: int
  wall_time: float


class FrameBuffer:
  """Holds the newest frame; readers can wait for the next one."""

  def __init__(self) -> None:
    self._cond = threading.Condition()
    self._frame: Frame | None = None
    self._seq = 0

  def put(self, data: bytes, content_type: str, width: int, height: int) -> Frame:
    with self._cond:
      self._seq += 1
      self._frame = Frame(self._seq, data, content_type, width, height, time.time())
      self._cond.notify_all()
      return self._frame

  def latest(self) -> Frame | None:
    with self._cond:
      return self._frame

  def wait_newer(self, seq: int, timeout_s: float) -> Frame | None:
    """The first frame with a sequence number above `seq`, or None after `timeout_s`."""
    with self._cond:
      self._cond.wait_for(lambda: self._frame is not None and self._frame.seq > seq, timeout=timeout_s)
      frame = self._frame
      return frame if frame is not None and frame.seq > seq else None


_FRAMES: dict[str, FrameBuffer] = {}


def frames(name: str) -> FrameBuffer | None:
  """The frame buffer of the running camera called `name` (for detector nodes and the like)."""
  return _FRAMES.get(name)


def encode_png_rgb(width: int, height: int, rows: Iterable[bytes]) -> bytes:
  """Minimal RGB PNG (8-bit, no filtering) using only zlib."""

  def chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

  raw = b"".join(b"\x00" + bytes(row) for row in rows)
  header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
  return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b"")


def color_bars(width: int, height: int, seq: int) -> bytes:
  """Colour bars with a sweeping marker, so a frozen stream is obvious."""
  bar_width = max(1, width // len(_BARS))
  base = bytearray()
  for x in range(width):
    base += bytes(_BARS[min(x // bar_width, len(_BARS) - 1)])
  marker = (seq * 4) % width
  rows = []
  band = height * 3 // 4
  for y in range(height):
    row = bytearray(base) if y < band else bytearray(b"\x20" * (3 * width))
    for x in range(marker, min(marker + 3, width)):
      row[3 * x : 3 * x + 3] = b"\xff\x40\x40" if y >= band else b"\x00\x00\x00"
    rows.append(row)
  return encode_png_rgb(width, height, rows)


class _BufferWriter(io.BufferedIOBase):
  """File-like sink for picamera2's FileOutput: each write is one encoded JPEG frame."""

  def __init__(self, on_frame: Callable[[bytes], None]) -> None:
    self._on_frame = on_frame

  def writable(self) -> bool:
    return True

  def write(self, data: Any) -> int:  # type: ignore[override]
    frame = bytes(data)
    self._on_frame(frame)
    return len(frame)


class CameraNode(LiveNode):
  """Pi camera, USB webcam, or a mock test pattern, streamed at `/camera/<node>.mjpg`."""

  def __init__(
    self,
    *,
    name: str = "front",
    width: int = 640,
    height: int = 480,
    fps: float = 15.0,
    source: str = "auto",
    device: int = 0,
    quality: int = 80,
    backend: str = "mock",
    optional: bool = False,
  ) -> None:
    """With `optional`, a missing or broken camera is reported as a warning and the rest
    of the robot keeps running without the stream."""
    if source not in SOURCES:
      raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    if not (16 <= width <= 4096 and 16 <= height <= 4096):
      raise ValueError("width and height must be between 16 and 4096")
    if not 0 < fps <= 120:
      raise ValueError("fps must be in (0, 120]")
    if not 1 <= quality <= 100:
      raise ValueError("quality must be 1..100")
    self.camera_name = name
    self.topic = f"camera.{name}"
    self.width = int(width)
    self.height = int(height)
    self.rate_hz = float(fps)
    self.source = source
    self.device = int(device)
    self.quality = int(quality)
    self.backend_name = backend
    self.optional = bool(optional)
    self.frame_buffer = FrameBuffer()
    self.active_source: str | None = None
    self._published_seq = 0
    self._frames = 0
    self._fps_window: list[float] = []
    self._stop = threading.Event()
    self._thread: threading.Thread | None = None
    self._camera: Any = None
    self._stale_reported = False
    self._started_wall = 0.0

  def _pick_source(self) -> str:
    if self.source != "auto":
      return self.source
    if resolve_backend(self.backend_name) == "mock":
      return "mock"
    if _importable("picamera2"):
      from picamera2 import Picamera2  # type: ignore[import-not-found]

      if Picamera2.global_camera_info():
        return "picamera2"
    if _importable("cv2"):
      return "opencv"
    raise HardwareUnavailable("no camera library found: sudo apt install python3-picamera2 (Pi camera) or python3-opencv (USB webcam)")

  def setup(self, ctx: NodeContext) -> None:
    self._started_wall = time.time()
    try:
      self.active_source = self._pick_source()
      if self.active_source == "picamera2":
        self._start_picamera2()
      elif self.active_source == "opencv":
        self._start_opencv()
    except Exception as exc:
      if not self.optional:
        raise
      self.active_source = "none"
      self._camera = None
      ctx.fault(f"camera {self.camera_name} unavailable, continuing without it: {exc}", kind="camera")
      return
    _FRAMES[self.camera_name] = self.frame_buffer

  def _start_picamera2(self) -> None:
    from picamera2 import Picamera2  # type: ignore[import-not-found]
    from picamera2.encoders import JpegEncoder  # type: ignore[import-not-found]
    from picamera2.outputs import FileOutput  # type: ignore[import-not-found]

    camera = Picamera2()
    camera.configure(camera.create_video_configuration(main={"size": (self.width, self.height)}, controls={"FrameRate": self.rate_hz}))
    writer = _BufferWriter(lambda data: self.frame_buffer.put(data, "image/jpeg", self.width, self.height))
    camera.start_recording(JpegEncoder(q=self.quality), FileOutput(writer))
    self._camera = camera

  def _start_opencv(self) -> None:
    import cv2  # type: ignore[import-not-found]

    capture = cv2.VideoCapture(self.device)
    if not capture.isOpened():
      raise HardwareUnavailable(f"cannot open camera device {self.device} (is a USB webcam plugged in? try `ls /dev/video*`)")
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
    capture.set(cv2.CAP_PROP_FPS, self.rate_hz)
    self._camera = capture
    params = [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]

    def loop() -> None:
      while not self._stop.is_set():
        ok, image = capture.read()
        if not ok:
          self._stop.wait(0.05)
          continue
        ok, encoded = cv2.imencode(".jpg", image, params)
        if ok:
          self.frame_buffer.put(encoded.tobytes(), "image/jpeg", int(image.shape[1]), int(image.shape[0]))

    self._thread = threading.Thread(target=loop, name=f"nervlynx-camera-{self.camera_name}", daemon=True)
    self._thread.start()

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    if self.active_source == "none":
      return None
    if self.active_source == "mock":
      width, height = min(self.width, _MOCK_MAX[0]), min(self.height, _MOCK_MAX[1])
      self.frame_buffer.put(color_bars(width, height, self._frames + 1), "image/png", width, height)
    frame = self.frame_buffer.latest()
    if frame is None or frame.seq == self._published_seq:
      waited = time.time() - (frame.wall_time if frame is not None else self._started_wall)
      if waited > _STALE_S and not self._stale_reported:
        self._stale_reported = True
        ctx.fault(f"camera {self.camera_name}: no new frame for {waited:.1f} s", kind="camera")
      return None
    self._stale_reported = False
    self._published_seq = frame.seq
    self._frames += 1
    now_s = ctx.now_ns / 1e9
    self._fps_window = [t for t in self._fps_window if now_s - t <= 2.0] + [now_s]
    return [
      (
        self.topic,
        "CameraFrame",
        {
          "seq": frame.seq,
          "width": frame.width,
          "height": frame.height,
          "bytes": len(frame.data),
          "fps": round(self._measured_fps(), 2),
          "format": frame.content_type.split("/")[-1],
        },
      )
    ]

  def _measured_fps(self) -> float:
    window = self._fps_window
    if len(window) < 2 or window[-1] == window[0]:
      return 0.0
    return (len(window) - 1) / (window[-1] - window[0])

  def teardown(self, ctx: NodeContext) -> None:
    self._stop.set()
    if self._thread is not None:
      self._thread.join(timeout=1.0)
    if self.active_source == "picamera2" and self._camera is not None:
      self._camera.stop_recording()
      self._camera.close()
    elif self.active_source == "opencv" and self._camera is not None:
      self._camera.release()
    self._camera = None
    if _FRAMES.get(self.camera_name) is self.frame_buffer:
      del _FRAMES[self.camera_name]

  def status(self) -> dict[str, Any]:
    latest = self.frame_buffer.latest()
    return {
      "camera": self.camera_name,
      "source": self.active_source or self.source,
      "frames": self._frames,
      "fps": round(self._measured_fps(), 2),
      "size": [latest.width, latest.height] if latest else [self.width, self.height],
    }
