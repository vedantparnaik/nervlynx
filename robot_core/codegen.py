from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_contract_idl(path: str | Path = "schemas/nervlynx_contracts.yaml") -> dict[str, Any]:
  p = Path(path)
  with p.open("r", encoding="utf-8") as f:
    return dict(yaml.safe_load(f) or {})


def generate_python_stub(idl: dict[str, Any], output: str | Path = "robot_core/generated_contracts.py") -> Path:
  lines = [
    "from __future__ import annotations",
    "",
    "from dataclasses import dataclass",
    "",
    "@dataclass(frozen=True)",
    "class GeneratedContract:",
    "  topic: str",
    "  schema: str",
    "  version: int",
    "  required_fields: tuple[str, ...]",
    "",
    "CONTRACTS = [",
  ]
  for c in idl.get("contracts", []):
    req = tuple(str(x) for x in c.get("required_fields", []))
    lines.append(
      f"  GeneratedContract(topic={c['topic']!r}, schema={c['schema']!r}, version={int(c['version'])}, required_fields={req!r}),"
    )
  lines.append("]")
  out = Path(output)
  out.parent.mkdir(parents=True, exist_ok=True)
  out.write_text("\n".join(lines) + "\n", encoding="utf-8")
  return out


def generate_cpp_stub(idl: dict[str, Any], output: str | Path = "cpp_core/include/nervlynx/generated_contracts.hpp") -> Path:
  lines = [
    "#pragma once",
    "",
    "#include <string>",
    "#include <vector>",
    "",
    "namespace nervlynx {",
    "struct GeneratedContract {",
    "  std::string topic;",
    "  std::string schema;",
    "  int version;",
    "  std::vector<std::string> required_fields;",
    "};",
    "",
    "inline std::vector<GeneratedContract> generated_contracts() {",
    "  return {",
  ]
  for c in idl.get("contracts", []):
    fields = ", ".join(f'\"{x}\"' for x in c.get("required_fields", []))
    lines.append(
      f'    GeneratedContract{{"{c["topic"]}", "{c["schema"]}", {int(c["version"])}, {{{fields}}}}},'
    )
  lines.extend(["  };", "}", "}", ""])
  out = Path(output)
  out.parent.mkdir(parents=True, exist_ok=True)
  out.write_text("\n".join(lines), encoding="utf-8")
  return out


def run_codegen() -> tuple[Path, Path]:
  idl = load_contract_idl()
  py = generate_python_stub(idl)
  cpp = generate_cpp_stub(idl)
  return py, cpp
