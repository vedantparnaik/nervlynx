import json
import threading
import time
from pathlib import Path

from robot_core.live import LiveNode, LiveRuntime
from robot_core.report import FaultLog, LineWriter, TraceRecorder
from robot_core.runtime import SimulatedClock


class StuckDisk:
  """Stands in for an SD card that stops answering until `release` is set."""

  def __init__(self, inner) -> None:
    self.inner = inner
    self.entered = threading.Event()
    self.release = threading.Event()

  def write(self, text: str) -> int:
    self.entered.set()
    self.release.wait(5.0)
    return self.inner.write(text)

  def flush(self) -> None:
    self.inner.flush()

  def close(self) -> None:
    self.inner.close()


class FullDisk:
  def write(self, text: str) -> int:
    raise OSError(28, "No space left on device")

  def flush(self) -> None:
    pass

  def close(self) -> None:
    pass


def jam(writer: LineWriter) -> StuckDisk:
  disk = StuckDisk(writer._file)
  writer._file = disk
  return disk


class Source(LiveNode):
  def tick(self, ctx):
    return [("cmd", "Cmd", {"v": 1})]


def test_a_stuck_disk_never_blocks_the_caller_and_drops_are_counted(tmp_path: Path) -> None:
  writer = LineWriter(tmp_path / "out.jsonl", name="test", max_buffer_bytes=100, flush_every_s=0.0)
  disk = jam(writer)
  writer.put("first\n")
  assert disk.entered.wait(2.0)
  started = time.monotonic()
  accepted = [writer.put("x" * 9 + "\n") for _ in range(50)]
  assert time.monotonic() - started < 0.5
  assert accepted.count(True) == 10 and writer.dropped == 40
  disk.release.set()
  writer.close()
  assert (tmp_path / "out.jsonl").read_text().splitlines() == ["first"] + ["x" * 9] * 10
  assert writer.written == 11 and writer.error is None


def test_blocking_mode_waits_for_the_disk_and_loses_nothing(tmp_path: Path) -> None:
  writer = LineWriter(tmp_path / "out.jsonl", name="test", max_buffer_bytes=100, flush_every_s=0.0, block_when_full=True)
  disk = jam(writer)
  writer.put("first\n")
  assert disk.entered.wait(2.0)
  done = threading.Event()

  def produce() -> None:
    for idx in range(50):
      writer.put(f"{idx:09d}\n")
    done.set()

  threading.Thread(target=produce, daemon=True).start()
  assert not done.wait(0.2)
  disk.release.set()
  assert done.wait(2.0)
  writer.close()
  assert writer.dropped == 0
  assert (tmp_path / "out.jsonl").read_text().splitlines() == ["first"] + [f"{idx:09d}" for idx in range(50)]


def test_flush_makes_everything_queued_readable_in_order(tmp_path: Path) -> None:
  path = tmp_path / "out.jsonl"
  writer = LineWriter(path, name="test", max_buffer_bytes=1 << 20, flush_every_s=60.0)
  for idx in range(500):
    writer.put(f"{idx}\n")
  assert writer.flush(2.0)
  assert path.read_text().splitlines() == [str(idx) for idx in range(500)]
  writer.close()
  writer.close()


def test_a_full_disk_is_reported_and_the_caller_keeps_going(tmp_path: Path) -> None:
  writer = LineWriter(tmp_path / "out.jsonl", name="test", max_buffer_bytes=1 << 20, flush_every_s=0.0)
  real, writer._file = writer._file, FullDisk()
  real.close()
  for _ in range(5):
    writer.put("x\n")
  assert writer.flush(2.0)
  assert writer.error is not None and "No space left" in writer.error
  assert writer.written == 0 and writer.dropped == 5
  assert writer.put("y\n") is False and writer.dropped == 6
  writer.close()


def test_a_stuck_trace_disk_does_not_slow_the_executor(tmp_path: Path) -> None:
  recorder = TraceRecorder(tmp_path / "trace.jsonl", max_buffer_bytes=4096)
  disk = jam(recorder._writer)
  rt = LiveRuntime(stall_timeout_s=0.2)
  rt.add_node("src", Source(), rate_hz=200)
  rt.add_message_listener(recorder)
  rt.run(duration_s=0.5)
  snap = rt.snapshot()
  assert snap["executor"]["stalls"] == 0
  assert snap["nodes"]["src"]["ticks"] >= 50
  assert recorder.dropped > 0
  disk.release.set()
  recorder.close()
  assert recorder.written + recorder.dropped == snap["messages"]["published"]


def test_trace_and_fault_log_from_a_simulated_run(tmp_path: Path) -> None:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("src", Source(), rate_hz=100)
  recorder = TraceRecorder(tmp_path / "trace.jsonl", exclude_topics=["skip"], block_when_full=True)
  faults = FaultLog(tmp_path / "faults.jsonl", block_when_full=True)
  rt.add_message_listener(recorder)
  rt.add_fault_listener(faults)
  rt.publish_external("skip", "Cmd", {})
  rt.request_estop("test", source="test")
  rt.run(duration_s=1.0)
  recorder.close()
  faults.close()
  lines = (tmp_path / "trace.jsonl").read_text().splitlines()
  assert len(lines) == recorder.written == rt.snapshot()["messages"]["published"] - 1
  assert all(json.loads(line)["topic"] != "skip" for line in lines)
  kinds = [json.loads(line)["kind"] for line in (tmp_path / "faults.jsonl").read_text().splitlines()]
  assert "estop" in kinds and faults.count == len(kinds) and faults.dropped == 0
