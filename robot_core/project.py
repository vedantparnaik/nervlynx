"""Robot projects: a live config plus optional `nodes/*.py` files next to it.

Any `@node` function or class in `<config dir>/nodes/*.py` becomes a plugin the config can
use by name, with no packaging or entry points.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from robot_core.builtin_plugins import register_builtin_plugins
from robot_core.live_config import load_live_config, register_live_builtins
from robot_core.node_api import node_spec
from robot_core.plugins import PluginRegistry
from robot_core.reference_plugins import register_reference_plugins

NODES_DIRNAME = "nodes"


def build_registry() -> PluginRegistry:
  """Entry-point plugins (or the built-in samples when none are installed) plus live nodes."""
  reg = PluginRegistry()
  reg.discover_entrypoints()
  if not reg.catalog().nodes and not reg.catalog().sensors:
    register_builtin_plugins(reg)
    register_reference_plugins(reg)
  register_live_builtins(reg)
  return reg


def _import_file(path: Path) -> ModuleType:
  digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]
  module_name = f"nervlynx_project_{digest}_{path.stem}"
  spec = importlib.util.spec_from_file_location(module_name, path)
  if spec is None or spec.loader is None:
    raise ImportError(f"cannot import {path}")
  module = importlib.util.module_from_spec(spec)
  sys.modules[module_name] = module
  try:
    spec.loader.exec_module(module)
  except BaseException:
    sys.modules.pop(module_name, None)
    raise
  return module


def register_project_nodes(registry: PluginRegistry, config_path: str | Path) -> list[str]:
  """Register every `@node` in `nodes/*.py` beside the config. Returns problems found."""
  nodes_dir = Path(config_path).resolve().parent / NODES_DIRNAME
  if not nodes_dir.is_dir():
    return []
  if str(nodes_dir) not in sys.path:
    sys.path.append(str(nodes_dir))
  problems: list[str] = []
  owners: dict[str, Path] = {}
  for path in sorted(nodes_dir.glob("*.py")):
    if path.name.startswith("_"):
      continue
    try:
      module = _import_file(path)
    except Exception as exc:  # noqa: BLE001 - user code can raise anything while importing
      problems.append(f"{path}: failed to load: {type(exc).__name__}: {exc}")
      continue
    for obj in vars(module).values():
      spec = node_spec(obj)
      if spec is None or getattr(obj, "__module__", None) != module.__name__:
        continue
      if spec.name in owners:
        problems.append(f"{path}: node {spec.name!r} is also defined in {owners[spec.name].name}")
        continue
      if registry.has_live_node(spec.name) or registry.has_node(spec.name) or registry.has_sensor(spec.name):
        problems.append(f"{path}: node {spec.name!r} clashes with a built-in plugin; rename it or pass @node(name=...)")
        continue
      owners[spec.name] = path
      registry.register_live_node(spec.name, spec.factory)
  return problems


def load_project(config_path: str | Path) -> tuple[dict[str, Any], PluginRegistry, list[str]]:
  """Config, a registry that includes the project's own nodes, and any node load problems."""
  registry = build_registry()
  problems = register_project_nodes(registry, config_path)
  return load_live_config(config_path), registry, problems
