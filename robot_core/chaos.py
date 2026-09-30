from __future__ import annotations

import zlib
from dataclasses import dataclass
from random import Random
from typing import Any

from robot_core.runtime import PipelineRuntime


@dataclass(frozen=True)
class ChaosConfig:
  drop_probability: float = 0.0
  mutate_probability: float = 0.0
  seed: int = 7


@dataclass(frozen=True)
class ChaosSummary:
  trials: int
  dropped: int
  mutated: int
  passed_through: int
  total_messages: int

  @property
  def drop_rate(self) -> float:
    return self.dropped / self.trials if self.trials else 0.0

  @property
  def mutate_rate(self) -> float:
    return self.mutated / self.trials if self.trials else 0.0


def _payload_seed(payload: dict[str, Any], seed: int) -> int:
  # zlib.crc32 is stable across processes; the builtin hash() of strings is not.
  return seed + zlib.crc32("\x1f".join(sorted(str(k) for k in payload.keys())).encode("utf-8"))


def _apply(payload: dict[str, Any], cfg: ChaosConfig, rng: Random) -> tuple[dict[str, Any] | None, bool]:
  if rng.random() < max(0.0, min(1.0, cfg.drop_probability)):
    return None, False
  out = dict(payload)
  if out and rng.random() < max(0.0, min(1.0, cfg.mutate_probability)):
    first_key = sorted(out.keys())[0]
    out[first_key] = 0
    return out, True
  return out, False


def inject_faults(payload: dict[str, Any], cfg: ChaosConfig) -> dict[str, Any] | None:
  out, _ = _apply(payload, cfg, Random(_payload_seed(payload, cfg.seed)))
  return out


def run_chaos_pass(runtime: PipelineRuntime, seed_topic: str, seed_payload: dict[str, Any], cfg: ChaosConfig) -> int:
  maybe = inject_faults(seed_payload, cfg)
  if maybe is None:
    return 0
  seed = runtime.publish(topic=seed_topic, source="chaos", schema="Seed", payload=maybe)
  trace = runtime.run_once(seed)
  return len(trace)


def run_chaos_trials(
  runtime: PipelineRuntime,
  seed_topic: str,
  seed_payload: dict[str, Any],
  cfg: ChaosConfig,
  trials: int,
) -> ChaosSummary:
  """Run many independent fault-injection trials from one deterministic RNG stream."""
  rng = Random(_payload_seed(seed_payload, cfg.seed))
  dropped = mutated = passed = total = 0
  for _ in range(max(0, trials)):
    maybe, was_mutated = _apply(seed_payload, cfg, rng)
    if maybe is None:
      dropped += 1
      continue
    if was_mutated:
      mutated += 1
    else:
      passed += 1
    total += len(runtime.run_once(runtime.publish(topic=seed_topic, source="chaos", schema="Seed", payload=maybe)))
  return ChaosSummary(trials=max(0, trials), dropped=dropped, mutated=mutated, passed_through=passed, total_messages=total)
