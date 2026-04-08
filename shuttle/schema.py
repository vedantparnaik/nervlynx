from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import capnp


@lru_cache(maxsize=1)
def load_schema():
  schema_path = Path(__file__).resolve().parents[1] / "schemas" / "shuttle.capnp"
  return capnp.load(str(schema_path))
