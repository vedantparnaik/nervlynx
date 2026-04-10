from __future__ import annotations

from dataclasses import dataclass
from random import Random
from typing import Any

from robot_core.runtime import PipelineRuntime


@dataclass(frozen=True)
class ChaosConfig:
  drop_probability: float = 0.0
  mutate_probability: float = 0.0
  seed: int = 7


def inject_faults(payload: dict[str, Any], cfg: ChaosConfig) -> dict[str, Any] | None:
  rng = Random(cfg.seed + hash(tuple(sorted(payload.keys()))))
  if rng.random() < max(0.0, min(1.0, cfg.drop_probability)):
    return None
  out = dict(payload)
  if out and rng.random() < max(0.0, min(1.0, cfg.mutate_probability)):
    first_key = sorted(out.keys())[0]
    out[first_key] = 0
  return out


def run_chaos_pass(runtime: PipelineRuntime, seed_topic: str, seed_payload: dict[str, Any], cfg: ChaosConfig) -> int:
  maybe = inject_faults(seed_payload, cfg)
  if maybe is None:
    return 0
  seed = runtime.publish(topic=seed_topic, source="chaos", schema="Seed", payload=maybe)
  trace = runtime.run_once(seed)
  return len(trace)
