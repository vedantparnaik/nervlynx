"""`detector`: object detection on camera frames, on a CPU, a Hailo accelerator, or an Orin NX.

A worker thread waits for each new frame from `frames(camera)` (a local camera, or one
shared from another device by the mesh), runs the model, and the node publishes the
result on `detections.<camera>`:

  {"seq": 812, "width": 640, "height": 480, "latency_ms": 23.1, "backend": "onnx",
   "model": "yolox-nano", "detections": [{"label": "person", "confidence": 0.87,
   "box": [x0, y0, x1, y1], "center": [cx, cy], "size": [w, h]}]}

Boxes are fractions of the image (x to the right, y down), so code works at any
resolution. The executor never waits on the model: a slow model lowers the detection
rate, never the control loop.

Backends (`backend:`):
  onnx      ONNX Runtime on the CPU (pip install "nervlynx[ai]")
  tensorrt  ONNX Runtime's TensorRT execution provider on NVIDIA Jetson (install
            onnxruntime-gpu for your JetPack); engines are cached, so only the first
            start is slow
  opencv    OpenCV's DNN module, where ONNX Runtime isn't available
  hailo     Hailo-8/8L (Raspberry Pi AI Kit and AI HAT+) through picamera2's Hailo
            helper, with a compiled .hef model
  auto      tensorrt on a Jetson, hailo for a .hef model, else onnx, else opencv
  mock      no model; publishes empty detections

Models (`model:`): `yolox-nano` (default) or `yolox-tiny`, Apache-2.0 models downloaded
once to ~/.cache/nervlynx/models (`nervlynx models get <name>` fetches them ahead of
time), or a path to your own .onnx (YOLOX and YOLOv8/YOLO11 exports are told apart by
their output shape) or .hef (a name looks in /usr/share/hailo-models too).
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from robot_core.hardware import HardwareUnavailable, _importable
from robot_core.live import LiveNode, NodeContext, Output

BACKENDS = ("auto", "onnx", "tensorrt", "opencv", "hailo", "mock")
COCO_LABELS = (
  "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
  "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
  "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
  "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
  "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
  "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
  "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard",
  "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
  "scissors", "teddy bear", "hair drier", "toothbrush",
)
_YOLOX_RELEASE = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0"
HAILO_MODEL_DIR = Path("/usr/share/hailo-models")
_AI_HINT = 'pip install "nervlynx[ai]" (numpy, onnxruntime, pillow)'


@dataclass(frozen=True)
class ModelInfo:
  name: str
  url: str
  sha256: str
  size_mb: float
  license: str


MODELS = {
  "yolox-nano": ModelInfo("yolox-nano", f"{_YOLOX_RELEASE}/yolox_nano.onnx", "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d", 3.7, "Apache-2.0"),
  "yolox-tiny": ModelInfo("yolox-tiny", f"{_YOLOX_RELEASE}/yolox_tiny.onnx", "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7", 20.2, "Apache-2.0"),
}


def model_dir() -> Path:
  return Path(os.environ.get("NERVLYNX_MODEL_DIR") or Path.home() / ".cache" / "nervlynx" / "models")


def fetch_model(name: str, *, directory: Path | None = None, echo: Any = None) -> Path:
  """The local file for a named model, downloading and checking it the first time."""
  info = MODELS.get(name)
  if info is None:
    raise ValueError(f"unknown model {name!r}; named models: {', '.join(MODELS)} (or give a path to a .onnx or .hef file)")
  folder = directory or model_dir()
  path = folder / f"{name}.onnx"
  if path.is_file():
    return path
  folder.mkdir(parents=True, exist_ok=True)
  if echo:
    echo(f"downloading {name} ({info.size_mb:.1f} MB, {info.license}) from {info.url}")
  partial = path.with_suffix(".part")
  digest = hashlib.sha256()
  try:
    with urllib.request.urlopen(info.url, timeout=60) as response, partial.open("wb") as out:
      while chunk := response.read(1 << 16):
        digest.update(chunk)
        out.write(chunk)
  except OSError as exc:
    partial.unlink(missing_ok=True)
    raise HardwareUnavailable(f"could not download {name} from {info.url} ({exc}); download it by hand to {path}") from exc
  if digest.hexdigest() != info.sha256:
    partial.unlink(missing_ok=True)
    raise HardwareUnavailable(f"{name} downloaded from {info.url} failed its checksum; try again, or download it by hand to {path}")
  partial.replace(path)
  return path


def resolve_model(model: str, *, download: bool = True) -> Path:
  path = Path(model).expanduser()
  if path.suffix in (".onnx", ".hef"):
    if path.is_file():
      return path
    if path.suffix == ".hef" and (HAILO_MODEL_DIR / path.name).is_file():
      return HAILO_MODEL_DIR / path.name
    raise HardwareUnavailable(f"model file {path} not found")
  if (HAILO_MODEL_DIR / f"{model}.hef").is_file():
    return HAILO_MODEL_DIR / f"{model}.hef"
  if model in MODELS:
    cached = model_dir() / f"{model}.onnx"
    if cached.is_file() or download:
      return fetch_model(model)
    raise HardwareUnavailable(f"model {model} is not downloaded yet: run `nervlynx models get {model}` (download: false is set)")
  raise ValueError(f"unknown model {model!r}; named models: {', '.join(MODELS)} (or give a path to a .onnx or .hef file)")


# ---------------------------------------------------------------------------- image and tensor helpers (numpy)


def decode_image(data: bytes) -> Any:
  """JPEG or PNG bytes to an RGB uint8 array (H, W, 3), with OpenCV or Pillow."""
  import numpy as np

  if _importable("cv2"):
    import cv2  # type: ignore[import-not-found]

    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
      raise ValueError("could not decode the camera frame")
    return image[:, :, ::-1]
  try:
    from io import BytesIO

    from PIL import Image  # type: ignore[import-not-found]
  except ImportError as exc:
    raise HardwareUnavailable(f"decoding camera frames needs OpenCV or Pillow: {_AI_HINT}") from exc
  return np.asarray(Image.open(BytesIO(data)).convert("RGB"))


def _resize(image: Any, width: int, height: int) -> Any:
  import numpy as np

  if _importable("cv2"):
    import cv2  # type: ignore[import-not-found]

    return cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR)
  if _importable("PIL"):
    from PIL import Image  # type: ignore[import-not-found]

    return np.asarray(Image.fromarray(image).resize((width, height), Image.BILINEAR))
  rows = (np.arange(height) * (image.shape[0] / height)).astype(int)
  cols = (np.arange(width) * (image.shape[1] / width)).astype(int)
  return image[rows][:, cols]  # nearest neighbour: coarser, but needs only numpy


def letterbox(image: Any, size: int, *, center: bool, pad_value: int = 114) -> tuple[Any, float, int, int]:
  """Scale `image` to fit a size x size square, padding the rest. Returns (image, scale, pad_x, pad_y)."""
  import numpy as np

  h, w = image.shape[:2]
  scale = min(size / h, size / w)
  nh, nw = max(1, int(round(h * scale))), max(1, int(round(w * scale)))
  canvas = np.full((size, size, 3), pad_value, dtype=np.uint8)
  pad_x, pad_y = ((size - nw) // 2, (size - nh) // 2) if center else (0, 0)
  canvas[pad_y : pad_y + nh, pad_x : pad_x + nw] = _resize(image, nw, nh)
  return canvas, scale, pad_x, pad_y


def decode_yolox(raw: Any, size: int, strides: Sequence[int] = (8, 16, 32)) -> tuple[Any, Any]:
  """YOLOX head output (N, 5 + classes), objectness and classes already sigmoided, to
  (boxes as x0, y0, x1, y1 in input pixels, per-class scores)."""
  import numpy as np

  grids, steps = [], []
  for stride in strides:
    cells = size // stride
    ys, xs = np.meshgrid(np.arange(cells), np.arange(cells), indexing="ij")
    grids.append(np.stack((xs, ys), axis=2).reshape(-1, 2))
    steps.append(np.full((cells * cells, 1), stride))
  grid, step = np.concatenate(grids).astype(np.float32), np.concatenate(steps).astype(np.float32)
  if raw.shape[0] != grid.shape[0]:
    raise ValueError(f"YOLOX output has {raw.shape[0]} rows but a {size}px input needs {grid.shape[0]}")
  centers = (raw[:, :2] + grid) * step
  sizes = np.exp(raw[:, 2:4]) * step
  boxes = np.concatenate((centers - sizes / 2, centers + sizes / 2), axis=1)
  return boxes, raw[:, 4:5] * raw[:, 5:]


def decode_yolov8(raw: Any) -> tuple[Any, Any]:
  """YOLOv8/YOLO11 output (4 + classes, N) to (boxes x0, y0, x1, y1, per-class scores)."""
  import numpy as np

  pred = raw.T
  centers, sizes = pred[:, :2], pred[:, 2:4]
  return np.concatenate((centers - sizes / 2, centers + sizes / 2), axis=1), pred[:, 4:]


def nms(boxes: Any, scores: Any, iou_threshold: float) -> list[int]:
  """Greedy non-maximum suppression; returns kept indices, best first."""
  import numpy as np

  order = np.argsort(-scores)
  areas = np.clip(boxes[:, 2] - boxes[:, 0], 0, None) * np.clip(boxes[:, 3] - boxes[:, 1], 0, None)
  keep: list[int] = []
  while order.size:
    best = int(order[0])
    keep.append(best)
    rest = order[1:]
    x0 = np.maximum(boxes[best, 0], boxes[rest, 0])
    y0 = np.maximum(boxes[best, 1], boxes[rest, 1])
    x1 = np.minimum(boxes[best, 2], boxes[rest, 2])
    y1 = np.minimum(boxes[best, 3], boxes[rest, 3])
    inter = np.clip(x1 - x0, 0, None) * np.clip(y1 - y0, 0, None)
    iou = inter / np.maximum(areas[best] + areas[rest] - inter, 1e-9)
    order = rest[iou <= iou_threshold]
  return keep


def select(boxes: Any, class_scores: Any, *, min_confidence: float, iou_threshold: float, max_detections: int = 100) -> list[tuple[int, float, Any]]:
  """Best class per box, confidence filter, then per-class NMS: [(class_id, score, box)]."""
  import numpy as np

  classes = np.argmax(class_scores, axis=1)
  scores = class_scores[np.arange(len(classes)), classes]
  mask = scores >= min_confidence
  boxes, scores, classes = boxes[mask], scores[mask], classes[mask]
  if not len(scores):
    return []
  shifted = boxes + (classes[:, None] * 10_000.0)
  keep = nms(shifted, scores, iou_threshold)[:max_detections]
  return [(int(classes[i]), float(scores[i]), boxes[i]) for i in keep]


def detection(label: str, confidence: float, x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
  """A detection in the published format, from a box in 0..1 image fractions."""
  x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in (x0, y0, x1, y1))
  return {
    "label": label,
    "confidence": round(float(confidence), 3),
    "box": [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)],
    "center": [round((x0 + x1) / 2, 4), round((y0 + y1) / 2, 4)],
    "size": [round(x1 - x0, 4), round(y1 - y0, 4)],
  }


# ---------------------------------------------------------------------------- backends


class _YoloBackend:
  """Shared pre- and post-processing for YOLOX and YOLOv8-style ONNX models."""

  name = "base"

  def __init__(self, model_path: Path, labels: Sequence[str]) -> None:
    self.model_path = model_path
    self.labels = tuple(labels)
    self.size = 416
    self.format = "yolox"

  def _set_format(self, input_shape: Sequence[Any], output_shape: Sequence[Any]) -> None:
    size = input_shape[-1]
    self.size = int(size) if isinstance(size, int) and size > 0 else 640
    dims = [int(d) if isinstance(d, int) else -1 for d in output_shape]
    # YOLOX: (1, rows, 5 + classes). YOLOv8/YOLO11: (1, 4 + classes, rows), rows >> classes.
    self.format = "yolov8" if len(dims) == 3 and dims[1] != -1 and dims[2] != -1 and dims[1] < dims[2] else "yolox"

  def _tensor(self, image: Any) -> tuple[Any, float, int, int]:
    import numpy as np

    center = self.format == "yolov8"
    boxed, scale, pad_x, pad_y = letterbox(image, self.size, center=center)
    if self.format == "yolox":
      data = boxed[:, :, ::-1].astype(np.float32)  # YOLOX was trained on BGR, 0..255
    else:
      data = boxed.astype(np.float32) / 255.0
    return np.ascontiguousarray(data.transpose(2, 0, 1)[None]), scale, pad_x, pad_y

  def _run(self, tensor: Any) -> Any:
    raise NotImplementedError

  def detect(self, image: Any, *, min_confidence: float, iou_threshold: float) -> list[dict[str, Any]]:
    tensor, scale, pad_x, pad_y = self._tensor(image)
    raw = self._run(tensor)[0]
    boxes, class_scores = decode_yolox(raw, self.size) if self.format == "yolox" else decode_yolov8(raw)
    h, w = image.shape[:2]
    out = []
    for class_id, score, box in select(boxes, class_scores, min_confidence=min_confidence, iou_threshold=iou_threshold):
      x0, y0, x1, y1 = ((box[0] - pad_x) / scale / w, (box[1] - pad_y) / scale / h, (box[2] - pad_x) / scale / w, (box[3] - pad_y) / scale / h)
      label = self.labels[class_id] if class_id < len(self.labels) else f"class {class_id}"
      out.append(detection(label, score, x0, y0, x1, y1))
    return out


class OnnxBackend(_YoloBackend):
  name = "onnx"

  def __init__(self, model_path: Path, labels: Sequence[str], *, providers: Sequence[Any] = ("CPUExecutionProvider",), threads: int | None = None) -> None:
    super().__init__(model_path, labels)
    try:
      import onnxruntime as ort  # type: ignore[import-not-found]
    except ImportError as exc:
      raise HardwareUnavailable(f"the onnx detector backend needs ONNX Runtime: {_AI_HINT}") from exc
    options = ort.SessionOptions()
    if threads:
      options.intra_op_num_threads = int(threads)
    self.session = ort.InferenceSession(str(model_path), sess_options=options, providers=list(providers))
    self.providers = self.session.get_providers()
    self.input_name = self.session.get_inputs()[0].name
    self._set_format(self.session.get_inputs()[0].shape, self.session.get_outputs()[0].shape)

  def _run(self, tensor: Any) -> Any:
    return self.session.run(None, {self.input_name: tensor})[0]


class TensorRtBackend(OnnxBackend):
  name = "tensorrt"

  def __init__(self, model_path: Path, labels: Sequence[str], *, threads: int | None = None) -> None:
    cache = str(model_dir() / "trt-cache")
    Path(cache).mkdir(parents=True, exist_ok=True)
    providers = [
      ("TensorrtExecutionProvider", {"trt_fp16_enable": True, "trt_engine_cache_enable": True, "trt_engine_cache_path": cache}),
      "CUDAExecutionProvider",
      "CPUExecutionProvider",
    ]
    super().__init__(model_path, labels, providers=providers, threads=threads)
    if "TensorrtExecutionProvider" not in self.providers:
      raise HardwareUnavailable(
        "ONNX Runtime here has no TensorRT provider; install onnxruntime-gpu built for your JetPack, or use backend: onnx"
      )


class OpenCvBackend(_YoloBackend):
  name = "opencv"

  def __init__(self, model_path: Path, labels: Sequence[str], *, input_size: int | None = None) -> None:
    super().__init__(model_path, labels)
    try:
      import cv2  # type: ignore[import-not-found]
    except ImportError as exc:
      raise HardwareUnavailable("the opencv detector backend needs OpenCV: sudo apt install python3-opencv") from exc
    self.net = cv2.dnn.readNetFromONNX(str(model_path))
    size = input_size or 416
    probe = self.net
    import numpy as np

    probe.setInput(np.zeros((1, 3, size, size), dtype=np.float32))
    self._set_format((1, 3, size, size), probe.forward().shape)

  def _run(self, tensor: Any) -> Any:
    self.net.setInput(tensor)
    return self.net.forward()


class HailoBackend:
  """A Hailo-8/8L through picamera2's Hailo helper, for .hef models with on-chip NMS."""

  name = "hailo"

  def __init__(self, model_path: Path, labels: Sequence[str]) -> None:
    try:
      from picamera2.devices import Hailo  # type: ignore[import-not-found]
    except ImportError as exc:
      raise HardwareUnavailable("the hailo backend needs the Hailo software: sudo apt install hailo-all, then reboot") from exc
    self.labels = tuple(labels)
    self.device = Hailo(str(model_path))
    self.height, self.width = self.device.get_input_shape()[:2]

  def detect(self, image: Any, *, min_confidence: float, iou_threshold: float) -> list[dict[str, Any]]:
    results = self.device.run(_resize(image, self.width, self.height))
    out = []
    for class_id, detections in enumerate(results):
      for row in detections:
        y0, x0, y1, x1, score = (float(v) for v in row[:5])
        if score >= min_confidence:
          label = self.labels[class_id] if class_id < len(self.labels) else f"class {class_id}"
          out.append(detection(label, score, x0, y0, x1, y1))
    return sorted(out, key=lambda d: -d["confidence"])

  def close(self) -> None:
    self.device.close()


class MockBackend:
  name = "mock"

  def detect(self, image: Any, *, min_confidence: float, iou_threshold: float) -> list[dict[str, Any]]:
    return []


def _is_jetson() -> bool:
  try:
    return "nvidia" in Path("/proc/device-tree/model").read_text(errors="replace").lower() or Path("/etc/nv_tegra_release").exists()
  except OSError:
    return Path("/etc/nv_tegra_release").exists()


def pick_backend(backend: str, model_path: Path | None) -> str:
  if backend != "auto":
    return backend
  if model_path is not None and model_path.suffix == ".hef":
    return "hailo"
  if _is_jetson() and _importable("onnxruntime"):
    import onnxruntime as ort  # type: ignore[import-not-found]

    if "TensorrtExecutionProvider" in ort.get_available_providers():
      return "tensorrt"
  if _importable("onnxruntime"):
    return "onnx"
  if _importable("cv2"):
    return "opencv"
  raise HardwareUnavailable(f"no detector backend available here: {_AI_HINT}")


def create_backend(backend: str, model_path: Path | None, labels: Sequence[str], *, threads: int | None = None, input_size: int | None = None) -> Any:
  if backend == "mock":
    return MockBackend()
  if model_path is None:
    raise HardwareUnavailable("no model to run")
  if backend == "hailo" or model_path.suffix == ".hef":
    if backend not in ("hailo", "auto"):
      raise HardwareUnavailable(f"{model_path.name} is a Hailo model; use backend: hailo")
    return HailoBackend(model_path, labels)
  if not _importable("numpy"):
    raise HardwareUnavailable(f"the detector needs numpy: {_AI_HINT}")
  if backend == "onnx":
    return OnnxBackend(model_path, labels, threads=threads)
  if backend == "tensorrt":
    return TensorRtBackend(model_path, labels, threads=threads)
  if backend == "opencv":
    return OpenCvBackend(model_path, labels, input_size=input_size)
  raise ValueError(f"unknown detector backend {backend!r}")


# ---------------------------------------------------------------------------- node


class Detector(LiveNode):
  """Runs a detection model on a camera's frames and publishes what it finds."""

  rate_hz = 30.0

  def __init__(
    self,
    *,
    camera: str = "front",
    backend: str = "auto",
    model: str = "yolox-nano",
    labels: list[str] | None = None,
    min_confidence: float = 0.5,
    iou_threshold: float = 0.45,
    max_fps: float = 10.0,
    topic: str | None = None,
    download: bool = True,
    threads: int | None = None,
    input_size: int | None = None,
    class_names: list[str] | None = None,
  ) -> None:
    """`labels` keeps only these classes (default: all); `class_names` replaces the COCO
    names for a custom model."""
    if backend not in BACKENDS:
      raise ValueError(f"backend must be one of {', '.join(BACKENDS)}")
    if not 0 < min_confidence < 1 or not 0 < iou_threshold < 1:
      raise ValueError("min_confidence and iou_threshold must be between 0 and 1")
    if not 0 < max_fps <= 60:
      raise ValueError("max_fps must be in (0, 60]")
    names = tuple(class_names) if class_names is not None else COCO_LABELS
    unknown = sorted(set(labels or ()) - set(names))
    if unknown:
      raise ValueError(f"labels not in the model's classes: {', '.join(unknown)}")
    self.camera = camera
    self.backend_name = backend
    self.model = model
    self.labels = frozenset(labels) if labels else None
    self.class_names = names
    self.min_confidence = float(min_confidence)
    self.iou_threshold = float(iou_threshold)
    self.max_fps = float(max_fps)
    self.topic = topic or f"detections.{camera}"
    self.download = bool(download)
    self.threads = threads
    self.input_size = input_size
    self.active_backend: str | None = None
    self._backend: Any = None
    self._lock = threading.Lock()
    self._stop = threading.Event()
    self._thread: threading.Thread | None = None
    self._latest: dict[str, Any] | None = None
    self._latest_seq = 0
    self._published_seq = 0
    self._error: str | None = None
    self._latencies: deque[float] = deque(maxlen=30)
    self._stamps: deque[float] = deque(maxlen=30)
    self._waiting_since = time.monotonic()
    self._missing_reported = False

  def setup(self, ctx: NodeContext) -> None:
    model_path = None if self.backend_name == "mock" else resolve_model(self.model, download=self.download)
    self.active_backend = pick_backend(self.backend_name, model_path)
    self._backend = create_backend(self.active_backend, model_path, self.class_names, threads=self.threads, input_size=self.input_size)
    if self.backend_name == "auto":
      ctx.fault(f"detector backend auto resolved to {self.active_backend}", severity="info", kind="detector")
    self._thread = threading.Thread(target=self._work, name=f"nervlynx-detector-{self.camera}", daemon=True)
    self._thread.start()

  def _work(self) -> None:
    from robot_core.camera import frames

    seq, last = 0, 0.0
    gap = 1.0 / self.max_fps
    while not self._stop.is_set():
      buffer = frames(self.camera)
      frame = buffer.wait_newer(seq, 0.5) if buffer is not None else None
      if frame is None:
        if buffer is None:
          self._stop.wait(0.2)
        continue
      seq = frame.seq
      now = time.monotonic()
      if now - last < gap:
        continue
      last = now
      started = time.perf_counter()
      try:
        image = decode_image(frame.data) if self.active_backend != "mock" else None
        found = self._backend.detect(image, min_confidence=self.min_confidence, iou_threshold=self.iou_threshold)
      except Exception as exc:  # noqa: BLE001 - reported from the executor thread
        self._error = f"{type(exc).__name__}: {exc}"
        self._stop.wait(1.0)
        continue
      latency = (time.perf_counter() - started) * 1000.0
      if self.labels is not None:
        found = [d for d in found if d["label"] in self.labels]
      payload = {
        "seq": frame.seq,
        "width": frame.width,
        "height": frame.height,
        "latency_ms": round(latency, 2),
        "backend": self.active_backend,
        "model": Path(self.model).stem if self.active_backend != "mock" else "mock",
        "detections": found,
      }
      with self._lock:
        self._latest = payload
        self._latest_seq += 1
        self._latencies.append(latency)
        self._stamps.append(now)
        self._waiting_since = now

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    if self._error is not None:
      error, self._error = self._error, None
      ctx.fault(f"detector: {error}", kind="detector", severity="error")
    with self._lock:
      payload, seq, since = self._latest, self._latest_seq, self._waiting_since
    if payload is None or seq == self._published_seq:
      waited = time.monotonic() - since
      if waited > 3.0 and not self._missing_reported:
        self._missing_reported = True
        ctx.fault(
          f"detector: no frames from camera {self.camera!r} for {waited:.0f} s "
          "(is its camera node running here, or shared from another device with mesh.frames?)",
          kind="detector",
        )
      return None
    self._missing_reported = False
    self._published_seq = seq
    return [(self.topic, "Detections", payload)]

  def teardown(self, ctx: NodeContext) -> None:
    self._stop.set()
    if self._thread is not None:
      self._thread.join(timeout=2.0)
    close = getattr(self._backend, "close", None)
    if close is not None:
      close()
    # Free the model now: some runtimes abort if their sessions outlive interpreter shutdown.
    self._backend = None

  def status(self) -> dict[str, Any]:
    with self._lock:
      latencies, stamps, latest = list(self._latencies), list(self._stamps), self._latest
    fps = (len(stamps) - 1) / (stamps[-1] - stamps[0]) if len(stamps) > 1 and stamps[-1] > stamps[0] else 0.0
    ordered = sorted(latencies)
    return {
      "detector": self.camera,
      "backend": self.active_backend or self.backend_name,
      "model": self.model,
      "fps": round(fps, 2),
      "latency_ms_p50": round(ordered[len(ordered) // 2], 2) if ordered else None,
      "found": [d["label"] for d in latest["detections"]] if latest else [],
    }
