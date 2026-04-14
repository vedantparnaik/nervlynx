from __future__ import annotations

import argparse
from pathlib import Path

from robot_core.examples import build_reference_runtime
from robot_core.recorder import read_jsonl, write_jsonl
from robot_core.runtime import SimulatedClock


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Validate deterministic replay against expected trace output.")
  parser.add_argument("--seed", type=Path, required=True, help="Path to seed trace jsonl with one input message.")
  parser.add_argument(
    "--expected",
    type=Path,
    required=True,
    help="Path to expected deterministic output trace jsonl.",
  )
  parser.add_argument(
    "--output",
    type=Path,
    default=Path("logs/replay_actual_trace.jsonl"),
    help="Path to write the generated replay trace.",
  )
  return parser.parse_args()


def normalize(path: Path) -> list[tuple[str, str, int, int, str, str, dict[str, object]]]:
  events = read_jsonl(path)
  return [
    (
      msg.envelope.topic,
      msg.envelope.source,
      msg.envelope.sequence,
      msg.envelope.monotonic_time_ns,
      msg.envelope.trace_id,
      msg.envelope.schema,
      msg.payload,
    )
    for msg in events
  ]


def main() -> None:
  args = parse_args()
  seed_events = read_jsonl(args.seed)
  if len(seed_events) != 1:
    raise SystemExit(f"seed trace must contain exactly one message, got {len(seed_events)}")

  clock = SimulatedClock(start_ns=1000)
  runtime = build_reference_runtime(clock=clock)
  actual = runtime.run_once(seed_events[0])
  write_jsonl(args.output, actual)

  expected_norm = normalize(args.expected)
  actual_norm = normalize(args.output)
  if actual_norm != expected_norm:
    raise SystemExit("deterministic replay mismatch: actual trace differs from expected fixture")

  print(f"deterministic_replay_ok events={len(actual_norm)} output={args.output}")


if __name__ == "__main__":
  main()
