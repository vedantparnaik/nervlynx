from __future__ import annotations

import compileall


def test_python_packages_bytecompile() -> None:
  """Catch syntax errors in shipped packages without importing every module."""
  assert compileall.compile_dir("robot_core", quiet=2)
  assert compileall.compile_dir("shuttle", quiet=2)
