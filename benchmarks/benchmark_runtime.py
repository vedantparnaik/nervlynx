from __future__ import annotations

import time

from robot_core.examples import build_reference_runtime


def main() -> None:
  rt = build_reference_runtime()
  loops = 5000
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
  print(f"loops={loops} messages={msg_count} elapsed_s={elapsed:.4f} msg_per_s={msg_count/elapsed:.2f}")


if __name__ == "__main__":
  main()
