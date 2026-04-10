from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class CheckpointStore:
  """Simple checkpoint persistence for node state recovery."""

  def __init__(self, root: str | Path = "state/checkpoints") -> None:
    self._root = Path(root)
    self._root.mkdir(parents=True, exist_ok=True)

  def save(self, node_name: str, state: dict[str, Any]) -> Path:
    path = self._root / f"{node_name}.json"
    with path.open("w", encoding="utf-8") as f:
      json.dump(state, f, separators=(",", ":"), sort_keys=True)
    return path

  def load(self, node_name: str) -> dict[str, Any] | None:
    path = self._root / f"{node_name}.json"
    if not path.exists():
      return None
    with path.open("r", encoding="utf-8") as f:
      raw = json.load(f)
    return dict(raw)
