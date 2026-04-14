from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import tracemalloc

from robot_core.examples import build_reference_runtime


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Benchmark NervLynx reference runtime.")
  parser.add_argument("--loops", type=int, default=5000, help="Number of seed messages to execute.")
  parser.add_argument(
    "--output-json",
    type=Path,
    default=None,
    help="Optional path to write benchmark metrics JSON.",
  )
  parser.add_argument(
    "--baseline",
    type=Path,
    default=None,
    help="Optional baseline JSON used for regression check.",
  )
  parser.add_argument(
    "--max-regression-pct",
    type=float,
    default=35.0,
    help="Maximum allowed throughput regression versus baseline.",
  )
  return parser.parse_args()


def run_benchmark(loops: int) -> dict[str, float]:
  rt = build_reference_runtime()
  tracemalloc.start()
  start = time.perf_counter()
  msg_count = 0
  for i in range(loops):
    seed = rt.publish(
      topic="sensors.raw",
      source="bench",
      schema="RawSensors",
      payload={"camera_count": 4, "gps_fix": (i % 2 == 0)},
    )
    msg_count += len(rt.run_once(seed))
  elapsed = time.perf_counter() - start
  _, peak_bytes = tracemalloc.get_traced_memory()
  tracemalloc.stop()
  msg_per_s = msg_count / elapsed if elapsed > 0 else 0.0
  return {
    "loops": float(loops),
    "messages": float(msg_count),
    "elapsed_s": elapsed,
    "msg_per_s": msg_per_s,
    "avg_loop_ms": (elapsed / loops) * 1000 if loops > 0 else 0.0,
    "peak_mem_mb": peak_bytes / (1024 * 1024),
  }


def compare_to_baseline(metrics: dict[str, float], baseline_path: Path, max_regression_pct: float) -> None:
  baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
  baseline_mps = float(baseline["metrics"]["msg_per_s"])
  current_mps = float(metrics["msg_per_s"])
  floor = baseline_mps * (1.0 - max_regression_pct / 100.0)
  if current_mps < floor:
    raise SystemExit(
      f"benchmark regression: msg_per_s={current_mps:.2f} below allowed floor={floor:.2f} "
      f"(baseline={baseline_mps:.2f}, max_regression_pct={max_regression_pct:.1f})"
    )
  delta_pct = ((current_mps - baseline_mps) / baseline_mps) * 100 if baseline_mps else 0.0
  print(
    f"baseline_check_ok current_msg_per_s={current_mps:.2f} baseline_msg_per_s={baseline_mps:.2f} "
    f"delta_pct={delta_pct:.2f}"
  )


def main() -> None:
  args = parse_args()
  metrics = run_benchmark(args.loops)
  print(
    "loops={loops:.0f} messages={messages:.0f} elapsed_s={elapsed_s:.4f} "
    "msg_per_s={msg_per_s:.2f} avg_loop_ms={avg_loop_ms:.4f} peak_mem_mb={peak_mem_mb:.3f}".format(**metrics)
  )

  if args.output_json is not None:
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    payload = {
      "benchmark": "reference_runtime",
      "metrics": metrics,
      "generated_at_epoch_s": time.time(),
    }
    args.output_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote_metrics_json={args.output_json}")

  if args.baseline is not None:
    compare_to_baseline(metrics, args.baseline, args.max_regression_pct)


if __name__ == "__main__":
  main()
