from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class TopicSpec:
  name: str
  hz: float
  critical: bool
  schema: str


@dataclass(frozen=True)
class BusConfig:
  publisher_address: str
  subscriber_address: str
  topics: dict[str, TopicSpec]


def _default_config_path() -> Path:
  return Path(__file__).resolve().parents[1] / "configs" / "services.yaml"


def load_config(path: str | Path | None = None) -> BusConfig:
  cfg_path = Path(path) if path else _default_config_path()
  with cfg_path.open("r", encoding="utf-8") as f:
    raw: dict[str, Any] = yaml.safe_load(f)

  topics: dict[str, TopicSpec] = {}
  for name, v in raw["topics"].items():
    topics[name] = TopicSpec(
      name=name,
      hz=float(v["hz"]),
      critical=bool(v["critical"]),
      schema=str(v["schema"]),
    )

  return BusConfig(
    publisher_address=str(raw["publisher_address"]),
    subscriber_address=str(raw["subscriber_address"]),
    topics=topics,
  )
