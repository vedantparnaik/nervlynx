from pathlib import Path

from robot_core.smoke_matrix import run_smoke_matrix


def test_smoke_matrix_all_cases_pass(tmp_path: Path) -> None:
  results = run_smoke_matrix(output_dir=tmp_path / "matrix")
  assert len(results) >= 5
  assert all(r.ok for r in results)
  assert all(r.output_path.exists() for r in results)
