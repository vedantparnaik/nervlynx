import subprocess
import sys

import pytest

from robot_core import transport


def test_core_cli_and_live_sim_never_import_zmq_or_capnp() -> None:
  script = "\n".join(
    [
      "import sys",
      "import robot_core.cli",
      "from robot_core.cli import _build_plugin_registry",
      "from robot_core.live_config import build_live_runtime, load_live_config",
      "from robot_core.runtime import SimulatedClock",
      "cfg = load_live_config('examples/live/rover_sim.yaml')",
      "runtime = build_live_runtime(cfg, _build_plugin_registry(), clock=SimulatedClock())",
      "runtime.run(duration_s=1.0)",
      "leaked = sorted(m for m in ('zmq', 'capnp') if m in sys.modules)",
      "print('leaked=' + ','.join(leaked))",
    ]
  )
  result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
  assert "leaked=\n" in result.stdout


def test_importing_nervlynx_stays_light_for_small_boards() -> None:
  script = "\n".join(
    [
      "import sys",
      "import nervlynx",
      "heavy = ['asyncio', 'http.server', 'importlib.metadata', 'robot_core.server', 'robot_core.codegen', 'robot_core.cli']",
      "print('loaded=' + ','.join(m for m in heavy if m in sys.modules))",
      "print('robot_core=' + str(sum(1 for m in sys.modules if m.startswith('robot_core'))))",
    ]
  )
  result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
  assert "loaded=\n" in result.stdout
  assert int(result.stdout.split("robot_core=")[1]) <= 6


def test_a_config_imports_only_the_drivers_it_uses() -> None:
  script = "\n".join(
    [
      "import sys",
      "from robot_core.live_config import build_live_runtime, load_live_config",
      "from robot_core.project import build_registry",
      "from robot_core.runtime import SimulatedClock",
      "reg = build_registry()",
      "assert reg.has_live_node('camera') and reg.has_live_node('esp32_link')",
      "build_live_runtime(load_live_config('examples/live/rover_sim.yaml'), reg, clock=SimulatedClock())",
      "print('loaded=' + ','.join(m for m in ('robot_core.camera', 'robot_core.link', 'robot_core.sensors') if m in sys.modules))",
    ]
  )
  result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
  assert "loaded=\n" in result.stdout


def test_lazy_factories_look_like_the_class_they_wrap() -> None:
  import inspect

  from robot_core.plugins import LazyFactory
  from robot_core.sensors import HCSR04Range

  factory = LazyFactory("robot_core.sensors:HCSR04Range")
  assert "backend" in inspect.signature(factory).parameters
  assert isinstance(factory(trigger=23, echo=24), HCSR04Range)
  with pytest.raises(ValueError, match="package.module:Name"):
    LazyFactory("robot_core.sensors")


def test_backend_params_always_mean_the_hardware_backend() -> None:
  import inspect

  from robot_core.hardware import BACKENDS
  from robot_core.live_config import LIVE_BUILTINS
  from robot_core.plugins import LazyFactory

  # hardware.backend is passed to every node with a `backend` parameter, so none may use it for anything else.
  for name, path in LIVE_BUILTINS.items():
    param = inspect.signature(LazyFactory(path)).parameters.get("backend")
    if param is not None:
      assert param.default in BACKENDS, f"{name}: backend={param.default!r} is not a hardware backend"


def test_zmq_transport_explains_the_missing_extra(monkeypatch) -> None:
  monkeypatch.setitem(sys.modules, "zmq", None)
  with pytest.raises(ModuleNotFoundError, match=r"nervlynx\[zmq\]"):
    transport.ZmqJsonTransport("tcp://127.0.0.1:1", "tcp://127.0.0.1:2")
