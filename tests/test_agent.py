import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from robot_core.agent import Agent, describe, rules_plan
from robot_core.live import LiveRuntime
from robot_core.live_config import build_live_runtime
from robot_core.project import build_registry
from robot_core.runtime import SimulatedClock


@pytest.mark.parametrize(
  ("text", "steps"),
  [
    ("turn left and drive forward one metre", [("turn", {"degrees": 90.0}), ("drive", {"distance_m": 1.0})]),
    ("Please turn right 45 degrees, then go back 30 cm", [("turn", {"degrees": -45.0}), ("drive", {"distance_m": -0.3})]),
    ("turn around", [("turn", {"degrees": 180.0})]),
    ("Stop!", [("stop", {})]),
    ("wait 3 seconds then find a dog", [("wait", {"seconds": 3.0}), ("find", {"label": "dog"})]),
    ("come here", [("find", {"label": "person"})]),
    ("turn left a bit and say hello and welcome", [("turn", {"degrees": 20.0}), ("say", {"text": "hello and welcome"})]),
    ("move forward two feet", [("drive", {"distance_m": 0.6096})]),
    ("back up", [("drive", {"distance_m": -0.5})]),
  ],
)
def test_rules_understand_everyday_commands(text, steps) -> None:
  assert [(s["skill"], s.get("args", {})) for s in rules_plan(text)] == [(n, pytest.approx(a) if a else a) for n, a in steps]


def test_rules_refuse_what_they_do_not_understand() -> None:
  with pytest.raises(ValueError, match="I don't know how to 'make me a sandwich'. I can: stop, drive"):
    rules_plan("turn left and make me a sandwich")
  assert describe([{"skill": "turn", "args": {"degrees": 90}}, {"skill": "drive", "args": {"distance_m": 1}}]) == "Turning left 90 degrees, then driving 1 metre."


def robot_with_agent(**agent) -> LiveRuntime:
  motors = {"left": [{"name": "left", "rpwm": 1, "lpwm": 2}], "right": [{"name": "right", "rpwm": 3, "lpwm": 4}]}
  cfg = {
    "nodes": [
      {"name": "drive", "plugin": "skid_steer_drive", "rate_hz": 50, "params": motors},
      {"name": "sim", "plugin": "skid_steer_sim", "rate_hz": 50, "params": {"world": {"width_m": 8, "height_m": 8}, "start": [4, 4, 0]}},
      {"name": "skills", "plugin": "skills"},
      {"name": "agent", "plugin": "agent", "params": agent},
    ]
  }
  return build_live_runtime(cfg, build_registry(), clock=SimulatedClock())


def run(rt: LiveRuntime, seconds: float) -> None:
  for _ in range(int(round(seconds / 0.02))):
    rt.clock.advance_ms(20)
    rt.step()


def test_a_typed_command_drives_the_simulated_robot() -> None:
  rt = robot_with_agent()
  replies: list = []
  rt.subscribe("probe", "agent.say", replies.append)
  rt.start()
  rt.publish_external("agent.command", "Command", {"text": "turn left then drive forward 1 meter"})
  run(rt, 12.0)
  assert replies[0].payload["text"] == "Turning left 90 degrees, then driving 1 metre." and replies[0].payload["id"] >= 1
  assert rt.nodes["skills"].status()["done"] == ["turned 90 degrees", "drove 1 m"]
  rt.publish_external("agent.command", "Command", {"text": "drive 10 meters"})
  run(rt, 0.1)
  assert "distance_m must be between -3 and 3 (got 10)" in replies[-1].payload["text"]


class FakeLlm(BaseHTTPRequestHandler):
  replies: list = []
  requests: list = []

  def log_message(self, *args):
    return

  def do_POST(self):  # noqa: N802
    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
    FakeLlm.requests.append((self.path, dict(self.headers), body))
    data = json.dumps(FakeLlm.replies.pop(0)).encode("utf-8")
    self.send_response(200)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)


@pytest.fixture()
def llm_server():
  FakeLlm.replies, FakeLlm.requests = [], []
  server = HTTPServer(("127.0.0.1", 0), FakeLlm)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  yield f"http://127.0.0.1:{server.server_address[1]}/v1"
  server.shutdown()
  server.server_close()


def tool_reply(*calls, content=None):
  return {"choices": [{"message": {"content": content, "tool_calls": [{"type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for n, a in calls]}}]}


def agent_runtime(base_url: str, **extra) -> tuple[LiveRuntime, list, list]:
  rt = LiveRuntime(clock=SimulatedClock(), seed=1)
  rt.add_node("agent", Agent(planner="llm", llm={"base_url": base_url, "model": "tiny", "timeout_s": 2}, about="a small rover", **extra))
  plans: list = []
  replies: list = []
  rt.subscribe("probe", "agent.plan", plans.append)
  rt.subscribe("probe2", "agent.say", replies.append)
  rt.start()
  return rt, plans, replies


def wait_for(rt: LiveRuntime, predicate) -> None:
  deadline = time.monotonic() + 5.0
  while not predicate():
    assert time.monotonic() < deadline, "timed out"
    rt.clock.advance_ms(100)
    rt.step()
    time.sleep(0.01)


def test_the_llm_can_only_answer_with_bounded_skills(llm_server, monkeypatch) -> None:
  monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
  FakeLlm.replies = [tool_reply(("turn", {"degrees": 90}), ("drive", {"distance_m": 0.5})), tool_reply(("drive", {"distance_m": 50}))]
  rt, plans, replies = agent_runtime(llm_server)
  rt.publish_external("agent.command", "Command", {"text": "have a look to your left, then scoot over"})
  wait_for(rt, lambda: plans)
  assert plans[0].payload["steps"] == [{"skill": "turn", "args": {"degrees": 90.0}}, {"skill": "drive", "args": {"distance_m": 0.5}}]
  path, headers, body = FakeLlm.requests[0]
  assert path == "/v1/chat/completions" and headers["Authorization"] == "Bearer sk-test" and body["model"] == "tiny"
  assert {t["function"]["name"] for t in body["tools"]} >= {"stop", "drive", "turn", "find"}
  assert "a small rover" in body["messages"][0]["content"] and body["messages"][1]["content"].startswith("have a look")
  rt.publish_external("agent.command", "Command", {"text": "drive to the kitchen"})
  wait_for(rt, lambda: len(replies) >= 2)
  assert len(plans) == 1 and "The model asked for something I can't do: drive: distance_m must be between -3 and 3" in replies[-1].payload["text"]


def test_a_model_that_only_talks_is_passed_on(llm_server) -> None:
  FakeLlm.replies = [{"choices": [{"message": {"content": "I can't climb stairs, sorry."}}]}]
  rt, plans, replies = agent_runtime(llm_server)
  rt.publish_external("agent.command", "Command", {"text": "go upstairs"})
  wait_for(rt, lambda: replies)
  assert plans == [] and replies[0].payload["text"] == "I can't climb stairs, sorry."


def test_an_unreachable_model_falls_back_to_simple_commands() -> None:
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
  rt, plans, replies = agent_runtime(f"http://127.0.0.1:{port}/v1")
  rt.publish_external("agent.command", "Command", {"text": "turn right"})
  wait_for(rt, lambda: replies)
  assert plans[0].payload["steps"] == [{"skill": "turn", "args": {"degrees": -90.0}}]
  assert replies[0].payload["text"].startswith("(The language model is unreachable")


def test_agent_settings_are_checked() -> None:
  with pytest.raises(ValueError, match="llm needs a model"):
    Agent(planner="llm")
  with pytest.raises(ValueError, match="unknown settings temp"):
    Agent(planner="llm", llm={"model": "x", "temp": 1})
