"""`nervlynx new`: create a robot project from a template in `robot_core/project_templates/`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent / "project_templates"
PLACEHOLDER = "__PROJECT_NAME__"
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")


@dataclass(frozen=True)
class Template:
  name: str
  summary: str


TEMPLATES: dict[str, Template] = {
  "obstacle-avoider": Template("obstacle-avoider", "drive forward, turn away from obstacles (HC-SR04 + L298N)"),
  "teleop": Template("teleop", "drive from the dashboard with W/A/S/D (L298N)"),
  "follow-me": Template("follow-me", "follow a person with a camera and a person detector (L298N)"),
}
DEFAULT_TEMPLATE = "obstacle-avoider"


class ScaffoldError(ValueError):
  pass


def create_project(name: str, template: str = DEFAULT_TEMPLATE, parent: Path = Path("."), *, force: bool = False) -> list[Path]:
  """Copy `template` to `parent/name`, filling in the project name. Returns the files written."""
  if not _NAME.match(name):
    raise ScaffoldError(f"project name {name!r} must start with a lowercase letter and use only a-z, 0-9, - and _")
  if template not in TEMPLATES:
    raise ScaffoldError(f"unknown template {template!r}; choose one of: {', '.join(TEMPLATES)}")
  source = TEMPLATES_DIR / template
  root = parent / name
  if root.exists() and any(root.iterdir()) and not force:
    raise ScaffoldError(f"{root} already exists and is not empty (use --force to overwrite)")
  written: list[Path] = []
  for path in sorted(source.rglob("*")):
    if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
      continue
    target = root / path.relative_to(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(path.read_text(encoding="utf-8").replace(PLACEHOLDER, name), encoding="utf-8")
    written.append(target)
  return written
