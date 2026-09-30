import json
import os
import subprocess
import sys

from robot_core.chaos import ChaosConfig, run_chaos_trials
from robot_core.examples import build_reference_runtime

_PROBE = """
import json
from robot_core.chaos import ChaosConfig, inject_faults
payload = {"camera_count": 4, "gps_fix": True}
print(json.dumps([inject_faults(payload, ChaosConfig(0.2, 0.2, seed)) for seed in range(40)]))
"""


def test_inject_faults_is_identical_across_hash_seeds() -> None:
  outputs = set()
  for hash_seed in ("0", "1", "4", "12345"):
    env = dict(os.environ, PYTHONHASHSEED=hash_seed)
    result = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True, env=env, check=True)
    outputs.add(result.stdout.strip())
  assert len(outputs) == 1
  decisions = json.loads(outputs.pop())
  assert any(d is None for d in decisions) and any(d is not None for d in decisions)


def test_chaos_trials_measure_configured_rates() -> None:
  summary = run_chaos_trials(
    build_reference_runtime(),
    "sensors.raw",
    {"camera_count": 4, "gps_fix": True},
    ChaosConfig(drop_probability=0.2, mutate_probability=0.2, seed=7),
    trials=2000,
  )
  assert summary.trials == summary.dropped + summary.mutated + summary.passed_through
  assert abs(summary.drop_rate - 0.2) < 0.03
  assert abs(summary.mutate_rate - 0.16) < 0.03
  assert summary.total_messages == 3 * (summary.trials - summary.dropped)
