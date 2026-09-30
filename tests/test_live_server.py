import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from robot_core.live import LiveNode, LiveRuntime
from robot_core.runtime import SimulatedClock
from robot_core.server import serve_live


class Sink(LiveNode):
  input_topics = ("cmd.drive",)

  def __init__(self) -> None:
    self.payloads: list[dict] = []

  def on_message(self, msg, ctx):
    self.payloads.append(msg.payload)


def request(base: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, str]:
  data = None if body is None else json.dumps(body).encode("utf-8")
  req = Request(base + path, data=data, headers=headers or {}, method="POST" if body is not None else "GET")
  try:
    with urlopen(req, timeout=3) as resp:
      return resp.status, resp.read().decode("utf-8")
  except HTTPError as exc:
    return exc.code, exc.read().decode("utf-8")


@pytest.fixture()
def live():
  rt = LiveRuntime(clock=SimulatedClock(), seed=1, name="srv")
  sink = rt.add_node("sink", Sink())
  rt.start()
  servers = []

  def start(**kwargs):
    server = serve_live(rt, host="127.0.0.1", port=0, **kwargs)
    servers.append(server)
    return f"http://127.0.0.1:{server.server_address[1]}"

  yield rt, sink, start
  for server in servers:
    server.shutdown()
    server.server_close()


def test_read_endpoints(live) -> None:
  rt, _, start = live
  base = start()
  code, body = request(base, "/health")
  assert code == 200 and json.loads(body)["status"] == "ok"
  graph = json.loads(request(base, "/graph")[1])
  assert graph["subscriptions"] == {"cmd.drive": 1}
  assert graph["nodes"]["sink"]["input_topics"] == ["cmd.drive"]
  stats = json.loads(request(base, "/stats")[1])
  assert stats["name"] == "srv" and "sink" in stats["nodes"]
  code, metrics = request(base, "/metrics")
  assert code == 200 and "# TYPE nervlynx_steps_total counter" in metrics
  code, page = request(base, "/")
  assert code == 200 and "<title>NervLynx live</title>" in page and '"allow_control": false' in page
  assert request(base, "/nope")[0] == 404
  assert json.loads(request(base, "/faults")[1]) == []


def test_estop_is_always_allowed_but_control_is_not(live) -> None:
  rt, _, start = live
  base = start()
  assert request(base, "/estop", {"reason": "test"})[0] == 200
  rt.step()
  assert rt.estop_engaged
  stats = json.loads(request(base, "/stats")[1])
  assert stats["faults"][-1]["kind"] == "estop" and stats["faults"][-1]["t_s"] == 0.0
  assert rt.snapshot(fault_limit=0)["faults"] == []
  assert request(base, "/estop/clear", {})[0] == 403
  assert request(base, "/publish", {"topic": "cmd.drive", "payload": {"left": 1, "right": 1}})[0] == 403


def test_control_requires_token_and_allowed_topic(live) -> None:
  rt, sink, start = live
  base = start(allow_control=True, control_token="t0k")
  command = {"topic": "cmd.drive", "schema": "DriveCommand", "payload": {"left": 0.2, "right": 0.2}}
  assert request(base, "/publish", command)[0] == 403
  assert request(base, "/publish", command, {"X-NervLynx-Token": "wrong"})[0] == 403
  assert request(base, "/publish?token=t0k", command)[0] == 200
  assert request(base, "/publish", {**command, "topic": "safety.estop"}, {"X-NervLynx-Token": "t0k"})[0] == 403
  assert request(base, "/publish", {**command, "payload": [1]}, {"X-NervLynx-Token": "t0k"})[0] == 400
  rt.step()
  assert sink.payloads == [{"left": 0.2, "right": 0.2}]
  request(base, "/estop", {})
  rt.step()
  assert request(base, "/estop/clear", {}, {"X-NervLynx-Token": "t0k"})[0] == 200
  rt.step()
  assert not rt.estop_engaged
  assert '"allow_control": true' in request(base, "/")[1]


def test_malformed_body_is_rejected(live) -> None:
  _, _, start = live
  base = start(allow_control=True)
  req = Request(base + "/publish", data=b"{not json", method="POST")
  with pytest.raises(HTTPError) as exc:
    urlopen(req, timeout=3)
  assert exc.value.code == 400
