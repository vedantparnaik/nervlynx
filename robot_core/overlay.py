"""Per-robot overlays that patch robot.yaml without editing it.

Two files beside robot.yaml are applied when present, in this order:

  overlay.yaml      per-robot settings, e.g. written by `nervlynx fleet deploy`
  calibration.yaml  written by the dashboard's calibration wizard

Both use the same shape. Nodes are addressed by name; mappings merge key by key, and
lists of named items (motors, servos) merge item by item on `name`:

  nodes:
    drive:
      params:
        left: [{name: left, invert: true}]
        tuning: {min_duty: 0.22}
  hardware: {backend: gpiozero}

Anything an overlay names must exist in robot.yaml, so a renamed motor fails loudly
instead of silently losing its calibration.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

OVERLAY_FILE = "overlay.yaml"
CALIBRATION_FILE = "calibration.yaml"
OVERLAY_FILES = (OVERLAY_FILE, CALIBRATION_FILE)
_CALIBRATION_HEADER = (
  "# Written by the NervLynx calibration wizard (dashboard > Calibrate).\n"
  "# It patches robot.yaml on this robot only; edit or delete it freely.\n"
)


class OverlayError(ValueError):
  pass


def _named_list(value: Any) -> bool:
  return isinstance(value, list) and bool(value) and all(isinstance(item, dict) and isinstance(item.get("name"), str) for item in value)


def merge(base: Any, patch: Any, *, where: str = "", strict: bool = True) -> Any:
  """`patch` applied to `base`. With `strict`, named list items must already exist."""
  if isinstance(base, dict) and isinstance(patch, dict):
    out = dict(base)
    for key, value in patch.items():
      out[key] = merge(base[key], value, where=f"{where}.{key}" if where else str(key), strict=strict) if key in base else value
    return out
  if _named_list(base) and _named_list(patch):
    out = [dict(item) for item in base]
    index = {item["name"]: i for i, item in enumerate(out)}
    for item in patch:
      name = item["name"]
      if name in index:
        out[index[name]] = merge(out[index[name]], item, where=f"{where}[{name}]", strict=strict)
      elif strict:
        known = ", ".join(sorted(index))
        raise OverlayError(f"{where}: robot.yaml has no item named {name!r} (it has: {known})")
      else:
        index[name] = len(out)
        out.append(dict(item))
    return out
  return patch


def _node_name(node: Any) -> Any:
  return node.get("name", node.get("plugin")) if isinstance(node, dict) else None


def apply_overlay(cfg: dict[str, Any], overlay: Any, *, source: str) -> dict[str, Any]:
  """A copy of `cfg` with `overlay` applied. Raises OverlayError naming `source`."""
  if overlay is None:
    return cfg
  if not isinstance(overlay, dict):
    raise OverlayError(f"{source}: must be a YAML mapping")
  out = dict(cfg)
  for key, value in overlay.items():
    if key == "nodes":
      continue
    try:
      out[key] = merge(cfg[key], value, where=key) if key in cfg else value
    except OverlayError as exc:
      raise OverlayError(f"{source}: {exc}") from None
  patches = overlay.get("nodes") or {}
  if not isinstance(patches, dict):
    raise OverlayError(f"{source}: nodes must map node names to settings, e.g. nodes: {{drive: {{params: ...}}}}")
  if not patches:
    return out
  nodes = cfg.get("nodes")
  if not isinstance(nodes, list):
    raise OverlayError(f"{source}: robot.yaml has no nodes list to patch")
  names = [_node_name(node) for node in nodes]
  patched = list(nodes)
  for name, patch in patches.items():
    if name not in names:
      raise OverlayError(f"{source}: node {name!r} is not in robot.yaml (nodes: {', '.join(str(n) for n in names)})")
    if not isinstance(patch, dict):
      raise OverlayError(f"{source}: nodes.{name} must be a mapping")
    if "name" in patch or "plugin" in patch:
      raise OverlayError(f"{source}: nodes.{name} cannot change a node's name or plugin")
    idx = names.index(name)
    try:
      patched[idx] = merge(nodes[idx], patch, where=f"nodes.{name}")
    except OverlayError as exc:
      raise OverlayError(f"{source}: {exc}") from None
  out["nodes"] = patched
  return out


def overlay_paths(config_path: str | Path) -> list[Path]:
  """The overlay files that exist beside `config_path`, in the order they apply."""
  folder = Path(config_path).resolve().parent
  return [folder / name for name in OVERLAY_FILES if (folder / name).is_file()]


def apply_overlays(cfg: dict[str, Any], config_path: str | Path) -> tuple[dict[str, Any], list[str]]:
  """`cfg` with every overlay beside `config_path` applied, and any problems found."""
  problems: list[str] = []
  for path in overlay_paths(config_path):
    try:
      with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
      cfg = apply_overlay(cfg, data, source=path.name)
    except (OSError, yaml.YAMLError, OverlayError) as exc:
      problems.append(f"{exc}" if isinstance(exc, OverlayError) else f"{path.name}: can't be read: {exc}")
  return cfg, problems


def write_calibration(config_path: str | Path, nodes: dict[str, dict[str, Any]]) -> Path:
  """Merge `nodes` patches into calibration.yaml beside `config_path` (atomically)."""
  path = Path(config_path).resolve().parent / CALIBRATION_FILE
  existing: dict[str, Any] = {}
  if path.is_file():
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(loaded, dict):
      existing = loaded
  merged = dict(existing)
  merged["nodes"] = merge(existing.get("nodes") or {}, nodes, where="nodes", strict=False)
  text = _CALIBRATION_HEADER + yaml.safe_dump(merged, sort_keys=False, default_flow_style=None)
  fd, tmp = tempfile.mkstemp(prefix=".calibration-", suffix=".yaml", dir=path.parent)
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
      f.write(text)
    os.replace(tmp, path)
  except BaseException:
    Path(tmp).unlink(missing_ok=True)
    raise
  return path
