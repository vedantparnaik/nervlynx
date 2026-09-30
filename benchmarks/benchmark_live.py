"""Benchmark the live executor.

1. Dispatch overhead: a source feeds a chain of nodes on a simulated clock, so the wall
   time is pure executor cost per delivered message (instrumentation included).
2. Tick precision: a node ticks at each requested rate on the real clock; reports how late
   ticks start (p50/p95/p99/max) and the CPU the executor used.

Run on the target hardware (e.g. the robot's Raspberry Pi) to characterise it.
"""

from __future__ import annotations

import argparse
import json
import platform
import time
from pathlib import Path

from robot_core.live import LiveNode, LiveRuntime
from robot_core.runtime import SimulatedClock


class _Source(LiveNode):
  def tick(self, ctx):
    return [("stage0", "Bench", {"v": 1})]


class _Stage(LiveNode):
  def __init__(self, idx: int) -> None:
    self.input_topics = (f"stage{idx}",)
    self.out = f"stage{idx + 1}"

  def on_message(self, msg, ctx):
    return [(self.out, "Bench", msg.payload)]


class _Idle(LiveNode):
  def tick(self, ctx):
    return None


def bench_dispatch(rate_hz: float, stages: int, sim_seconds: float) -> dict[str, float]:
  rt = LiveRuntime(clock=SimulatedClock(), seed=0)
  rt.add_node("source", _Source(), rate_hz=rate_hz)
  for idx in range(stages):
    rt.add_node(f"stage{idx}", _Stage(idx))
  started = time.perf_counter()
  rt.run(duration_s=sim_seconds)
  elapsed = time.perf_counter() - started
  snap = rt.snapshot()
  delivered = snap["messages"]["delivered"]
  return {
    "rate_hz": rate_hz,
    "stages": float(stages),
    "sim_seconds": sim_seconds,
    "wall_s": round(elapsed, 4),
    "published": float(snap["messages"]["published"]),
    "delivered": float(delivered),
    "deliveries_per_s": round(delivered / elapsed, 1) if elapsed else 0.0,
    "us_per_delivery": round(elapsed / delivered * 1e6, 3) if delivered else 0.0,
    "realtime_factor": round(sim_seconds / elapsed, 1) if elapsed else 0.0,
  }


def bench_ticks(rate_hz: float, seconds: float) -> dict[str, float | None]:
  rt = LiveRuntime(stall_timeout_s=None)
  rt.add_node("loop", _Idle(), rate_hz=rate_hz)
  cpu0, wall0 = time.process_time(), time.perf_counter()
  rt.run(duration_s=seconds)
  cpu, wall = time.process_time() - cpu0, time.perf_counter() - wall0
  node = rt.snapshot()["nodes"]["loop"]
  late = node["lateness_ms"]
  return {
    "rate_hz": rate_hz,
    "seconds": seconds,
    "ticks": float(node["ticks"]),
    "expected_ticks": float(int(rate_hz * seconds) + 1),
    "overruns": float(node["overruns"]),
    "lateness_p50_ms": late["p50"],
    "lateness_p95_ms": late["p95"],
    "lateness_p99_ms": late["p99"],
    "lateness_max_ms": late["max"],
    "cpu_pct_of_one_core": round(100.0 * cpu / wall, 2) if wall else 0.0,
  }


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--stages", type=int, default=4, help="Nodes chained after the source.")
  parser.add_argument("--source-hz", type=float, default=1000.0)
  parser.add_argument("--sim-seconds", type=float, default=20.0)
  parser.add_argument("--tick-rates", type=float, nargs="*", default=[50.0, 100.0, 200.0])
  parser.add_argument("--tick-seconds", type=float, default=5.0)
  parser.add_argument("--output-json", type=Path, default=None)
  args = parser.parse_args()

  dispatch = bench_dispatch(args.source_hz, args.stages, args.sim_seconds)
  print(
    "dispatch stages={stages:.0f} delivered={delivered:.0f} wall_s={wall_s} deliveries_per_s={deliveries_per_s} "
    "us_per_delivery={us_per_delivery} realtime_factor={realtime_factor}".format(**dispatch)
  )
  ticks = []
  for rate in args.tick_rates:
    result = bench_ticks(rate, args.tick_seconds)
    ticks.append(result)
    print(
      "ticks rate_hz={rate_hz:.0f} ticks={ticks:.0f}/{expected_ticks:.0f} overruns={overruns:.0f} "
      "lateness_ms p50={lateness_p50_ms} p95={lateness_p95_ms} p99={lateness_p99_ms} max={lateness_max_ms} "
      "cpu_pct={cpu_pct_of_one_core}".format(**result)
    )
  if args.output_json is not None:
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    payload = {
      "benchmark": "live_executor",
      "platform": {"python": platform.python_version(), "system": platform.system(), "machine": platform.machine()},
      "dispatch": dispatch,
      "ticks": ticks,
      "generated_at_epoch_s": time.time(),
    }
    args.output_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote_metrics_json={args.output_json}")


if __name__ == "__main__":
  main()
