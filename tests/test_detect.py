import hashlib
import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from robot_core import detect
from robot_core.camera import FrameBuffer, register_frames, unregister_frames
from robot_core.detect import COCO_LABELS, Detector, ModelInfo, detection, fetch_model, resolve_model
from robot_core.hardware import HardwareUnavailable
from robot_core.live import LiveRuntime
from robot_core.live_config import validate_live_config
from robot_core.nervlynx_cli import app
from robot_core.project import build_registry
from robot_core.runtime import SimulatedClock

np = pytest.importorskip("numpy")


def test_yolox_output_is_decoded_on_its_grid() -> None:
  raw = np.zeros((3549, 85), dtype=np.float32)
  row = 5 * 52 + 10  # stride-8 grid, cell x=10, y=5
  raw[row, :4] = [0.5, 0.5, np.log(4.0), np.log(2.0)]
  raw[row, 4], raw[row, 5] = 0.9, 0.8
  boxes, scores = detect.decode_yolox(raw, 416)
  assert boxes[row].tolist() == pytest.approx([68.0, 36.0, 100.0, 52.0])
  assert scores[row, 0] == pytest.approx(0.72)
  with pytest.raises(ValueError, match="needs 8400"):
    detect.decode_yolox(raw, 640)


def test_yolov8_output_is_transposed_and_decoded() -> None:
  raw = np.zeros((84, 8400), dtype=np.float32)
  raw[:4, 7] = [320, 240, 100, 50]
  raw[4 + 2, 7] = 0.6
  boxes, scores = detect.decode_yolov8(raw)
  assert boxes[7].tolist() == [270.0, 215.0, 370.0, 265.0] and scores[7, 2] == pytest.approx(0.6)


def test_select_keeps_the_best_box_per_object_and_class() -> None:
  boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [0, 0, 10, 10], [50, 50, 60, 60]], dtype=np.float32)
  scores = np.zeros((4, 3), dtype=np.float32)
  scores[0, 0], scores[1, 0], scores[2, 1], scores[3, 0] = 0.9, 0.8, 0.7, 0.3
  picked = detect.select(boxes, scores, min_confidence=0.5, iou_threshold=0.45)
  assert [(c, round(s, 2)) for c, s, _ in picked] == [(0, 0.9), (1, 0.7)]


def test_letterbox_pads_like_each_model_family_expects(monkeypatch) -> None:
  image = np.full((100, 200, 3), 7, dtype=np.uint8)
  boxed, scale, pad_x, pad_y = detect.letterbox(image, 416, center=False)
  assert boxed.shape == (416, 416, 3) and scale == pytest.approx(2.08) and (pad_x, pad_y) == (0, 0)
  assert boxed[0, 0, 0] == 7 and boxed[415, 0, 0] == 114
  _, _, pad_x, pad_y = detect.letterbox(image, 416, center=True)
  assert (pad_x, pad_y) == (0, 104)
  monkeypatch.setattr(detect, "_importable", lambda module: False)  # numpy only
  assert detect._resize(np.arange(12, dtype=np.uint8).reshape(2, 2, 3), 4, 4).shape == (4, 4, 3)


def test_detections_are_plain_json_fractions() -> None:
  d = detection("person", np.float32(0.87654), np.float32(-0.1), 0.2, 0.6, 1.3)
  assert d == {"label": "person", "confidence": 0.877, "box": [0.0, 0.2, 0.6, 1.0], "center": [0.3, 0.6], "size": [0.6, 0.8]}
  json.dumps(d)


def test_named_models_are_downloaded_once_and_checked(tmp_path, monkeypatch) -> None:
  source = tmp_path / "model.onnx"
  source.write_bytes(b"not really a model")
  good = ModelInfo("tiny", source.as_uri(), hashlib.sha256(b"not really a model").hexdigest(), 0.1, "MIT")
  monkeypatch.setitem(detect.MODELS, "tiny", good)
  monkeypatch.setitem(detect.MODELS, "broken", ModelInfo("broken", source.as_uri(), "0" * 64, 0.1, "MIT"))
  cache = tmp_path / "cache"
  monkeypatch.setenv("NERVLYNX_MODEL_DIR", str(cache))
  path = fetch_model("tiny")
  assert path == cache / "tiny.onnx" and path.read_bytes() == b"not really a model"
  source.write_bytes(b"changed")
  assert fetch_model("tiny").read_bytes() == b"not really a model"  # cached
  with pytest.raises(HardwareUnavailable, match="failed its checksum"):
    fetch_model("broken")
  assert not list(cache.glob("*.part"))
  with pytest.raises(HardwareUnavailable, match="nervlynx models get broken"):
    resolve_model("broken", download=False)
  with pytest.raises(ValueError, match="unknown model 'yolo-huge'"):
    resolve_model("yolo-huge")
  listing = CliRunner().invoke(app, ["models"])
  assert "tiny" in listing.stdout and "downloaded" in listing.stdout and str(cache) in listing.stdout


class FixedBackend:
  name = "fixed"

  def __init__(self):
    self.calls = 0

  def detect(self, image, *, min_confidence, iou_threshold):
    self.calls += 1
    return [detection("person", 0.9, 0.4, 0.2, 0.6, 0.9), detection("dog", 0.8, 0.1, 0.5, 0.2, 0.7)]


def test_the_node_runs_the_model_off_the_executor_and_filters_labels(monkeypatch) -> None:
  backend = FixedBackend()
  monkeypatch.setattr(detect, "resolve_model", lambda model, download=True: Path("/models/x.onnx"))
  monkeypatch.setattr(detect, "create_backend", lambda *a, **k: backend)
  monkeypatch.setattr(detect, "decode_image", lambda data: np.zeros((48, 64, 3), dtype=np.uint8))
  node = Detector(camera="bench", backend="onnx", labels=["person"], max_fps=60)
  buffer = register_frames("bench", FrameBuffer())
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("detector", node)
  seen: list = []
  rt.subscribe("probe", "detections.bench", seen.append)
  rt.start()
  try:
    buffer.put(b"jpeg", "image/jpeg", 64, 48)
    deadline = time.monotonic() + 3.0
    while not seen and time.monotonic() < deadline:
      rt.clock.advance_ms(33)
      rt.step()
      time.sleep(0.005)
    payload = seen[0].payload
    assert payload["detections"] == [detection("person", 0.9, 0.4, 0.2, 0.6, 0.9)]
    assert (payload["width"], payload["height"], payload["backend"]) == (64, 48, "onnx")
    assert node.status()["found"] == ["person"]
  finally:
    rt.shutdown()
    unregister_frames("bench", buffer)


def test_a_detector_without_frames_says_where_to_look(monkeypatch) -> None:
  node = Detector(camera="nowhere", backend="mock")
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("detector", node)
  rt.start()
  node._waiting_since -= 5.0
  rt.step()
  rt.shutdown()
  assert any("no frames from camera 'nowhere'" in e.message and "mesh.frames" in e.message for e in rt.fault_events)


def test_detector_config_is_checked_without_loading_a_model() -> None:
  reg = build_registry()
  assert validate_live_config({"nodes": [{"plugin": "detector", "params": {"camera": "front", "labels": ["person"]}}]}, reg) == []
  bad = validate_live_config({"nodes": [{"plugin": "detector", "params": {"labels": ["unicorn"]}}]}, reg)
  assert bad == ["nodes[0] (detector): labels not in the model's classes: unicorn"]
  assert "person" in COCO_LABELS and len(COCO_LABELS) == 80


def test_real_yolox_nano_finds_a_person_if_downloaded() -> None:
  model = detect.model_dir() / "yolox-nano.onnx"
  sample = Path("/tmp/nlx-models/person.jpg")
  if not model.is_file() or not sample.is_file():
    pytest.skip("yolox-nano not downloaded (nervlynx models get yolox-nano) or no sample image")
  pytest.importorskip("onnxruntime")
  backend = detect.OnnxBackend(model, COCO_LABELS)
  found = backend.detect(detect.decode_image(sample.read_bytes()), min_confidence=0.5, iou_threshold=0.45)
  assert found[0]["label"] == "person" and found[0]["confidence"] > 0.8
