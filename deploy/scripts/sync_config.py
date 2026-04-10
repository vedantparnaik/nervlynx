from __future__ import annotations

from pathlib import Path
import shutil
import sys


def main() -> None:
  if len(sys.argv) != 3:
    raise SystemExit("usage: python deploy/scripts/sync_config.py <source_yaml> <target_dir>")
  source = Path(sys.argv[1])
  target_dir = Path(sys.argv[2])
  target_dir.mkdir(parents=True, exist_ok=True)
  target = target_dir / source.name
  shutil.copy2(source, target)
  print(f"synced {source} -> {target}")


if __name__ == "__main__":
  main()
