from pathlib import Path

from robot_core.codegen import generate_cpp_stub, generate_python_stub, load_contract_idl


def test_codegen_generates_python_and_cpp_files(tmp_path: Path) -> None:
  idl = load_contract_idl("schemas/nervlynx_contracts.yaml")
  py = generate_python_stub(idl, tmp_path / "generated_contracts.py")
  cpp = generate_cpp_stub(idl, tmp_path / "generated_contracts.hpp")
  assert py.exists()
  assert cpp.exists()
  assert "GeneratedContract" in py.read_text(encoding="utf-8")
  assert "generated_contracts" in cpp.read_text(encoding="utf-8")
