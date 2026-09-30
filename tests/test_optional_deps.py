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


def test_zmq_transport_explains_the_missing_extra(monkeypatch) -> None:
  monkeypatch.setitem(sys.modules, "zmq", None)
  with pytest.raises(ModuleNotFoundError, match=r"nervlynx\[zmq\]"):
    transport.ZmqJsonTransport("tcp://127.0.0.1:1", "tcp://127.0.0.1:2")
