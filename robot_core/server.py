"""HTTP control and observability surface for a running `LiveRuntime`.

GET  /           live dashboard (HTML)
GET  /metrics    Prometheus text exposition
GET  /health     status summary
GET  /graph      nodes, topics, and subscriptions
GET  /stats      full runtime snapshot (JSON)
GET  /faults     recent structured faults
GET  /camera/<node>.mjpg     live stream of a camera node (multipart, one image per part)
GET  /camera/<node>/latest   the newest frame of a camera node
GET  /calibration            what each calibratable node can do, and the saved calibration
POST /estop      latch the e-stop (always allowed)
POST /estop/clear            requires control access
POST /publish {topic, schema, payload}   requires control access and an allowed topic
POST /calibration/<node> {action, ...}   one calibration-wizard step; requires control access
POST /calibration/save                   write calibration.yaml; requires control access

Control access means the server was started with `allow_control=True` and, if a token
is configured, the request carries it (header `X-NervLynx-Token` or `?token=`).
"""

from __future__ import annotations

import hmac
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlparse

from robot_core.live import LiveRuntime, LiveRuntimeError, supports_calibration

_MAX_BODY_BYTES = 64 * 1024
_CAMERA_PATH = re.compile(r"^/camera/([A-Za-z0-9_.-]+?)(\.mjpg|/latest)$")
_CALIBRATION_PATH = re.compile(r"^/calibration/([^/]+)$")
_BOUNDARY = "nervlynxframe"


class LiveHTTPServer(ThreadingHTTPServer):
  """Threading server whose long-lived handlers (camera streams) end on shutdown."""

  daemon_threads = True

  def __init__(self, *args: Any, **kwargs: Any) -> None:
    super().__init__(*args, **kwargs)
    self.stopping = Event()

  def shutdown(self) -> None:
    self.stopping.set()
    super().shutdown()


def serve_live(
  runtime: LiveRuntime,
  *,
  host: str = "127.0.0.1",
  port: int = 9120,
  allow_control: bool = False,
  control_topics: Iterable[str] = ("cmd.drive",),
  control_token: str | None = None,
  config_path: str | Path | None = None,
) -> ThreadingHTTPServer:
  """`config_path` (robot.yaml) is where the calibration wizard saves calibration.yaml;
  without it the wizard still works but cannot save."""
  topics = tuple(control_topics)
  page = _DASHBOARD_HTML.replace(
    "__CONFIG__",
    json.dumps({"allow_control": allow_control, "drive_topic": topics[0] if allow_control and topics else None}),
  ).encode("utf-8")

  class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
      return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
      self.send_response(code)
      self.send_header("Content-Type", content_type)
      self.send_header("Content-Length", str(len(body)))
      self.send_header("Cache-Control", "no-store")
      self.end_headers()
      self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
      self._send(code, json.dumps(obj, default=str).encode("utf-8"), "application/json")

    def _authorised(self) -> bool:
      if not allow_control:
        return False
      if not control_token:
        return True
      supplied = self.headers.get("X-NervLynx-Token") or parse_qs(urlparse(self.path).query).get("token", [""])[0]
      return hmac.compare_digest(supplied.encode("utf-8"), control_token.encode("utf-8"))

    def _body(self) -> dict[str, Any] | None:
      try:
        length = int(self.headers.get("Content-Length") or 0)
      except ValueError:
        return None
      if length > _MAX_BODY_BYTES:
        return None
      raw = self.rfile.read(length) if length else b""
      if not raw.strip():
        return {}
      try:
        data = json.loads(raw)
      except json.JSONDecodeError:
        return None
      return data if isinstance(data, dict) else None

    def do_GET(self) -> None:  # noqa: N802
      path = urlparse(self.path).path
      if path in ("/", "/index.html"):
        self._send(200, page, "text/html; charset=utf-8")
      elif path == "/metrics":
        self._send(200, runtime.metrics.render_prometheus().encode("utf-8"), "text/plain; version=0.0.4")
      elif path == "/health":
        self._json(runtime.health())
      elif path == "/graph":
        snap = runtime.snapshot(fault_limit=0)
        self._json(
          {
            "name": snap["name"],
            "subscriptions": {topic: len(nodes) for topic, nodes in runtime.subscriptions.items()},
            "node_heartbeats_count": len(runtime.node_heartbeats_ns),
            "fault_count": len(runtime.faults),
            "nodes": {name: {k: node[k] for k in ("input_topics", "rate_hz", "critical")} for name, node in snap["nodes"].items()},
            "topics": {topic: {"subscribers": t["subscribers"], "rate_hz": t["rate_hz"]} for topic, t in snap["topics"].items()},
          }
        )
      elif path == "/stats":
        self._json(runtime.snapshot())
      elif path == "/faults":
        self._json([event.to_dict() for event in runtime.fault_events])
      elif _CAMERA_PATH.match(path):
        self._camera(*_CAMERA_PATH.match(path).groups())
      elif path == "/calibration":
        self._calibration_overview()
      else:
        self._json({"error": "not found"}, 404)

    def _calibratable(self) -> list[str]:
      return [name for name, node in runtime.nodes.items() if supports_calibration(node)]

    def _calibration_overview(self) -> None:
      nodes: dict[str, Any] = {}
      for name in self._calibratable():
        try:
          nodes[name] = runtime.call_node(name, lambda node, ctx: node.calibrate("describe", {}, ctx))
        except (TimeoutError, ValueError, LiveRuntimeError) as exc:
          nodes[name] = {"error": str(exc)}
      saved = None
      if config_path is not None:
        import yaml

        from robot_core.overlay import CALIBRATION_FILE

        file = Path(config_path).resolve().parent / CALIBRATION_FILE
        if file.is_file():
          try:
            saved = yaml.safe_load(file.read_text(encoding="utf-8"))
          except (OSError, yaml.YAMLError) as exc:
            saved = {"error": str(exc)}
      self._json({"nodes": nodes, "can_save": config_path is not None and self._authorised(), "saved": saved})

    def _calibrate(self, name: str, body: dict[str, Any]) -> None:
      if name not in self._calibratable():
        self._json({"error": f"no calibratable node named {name!r}"}, 404)
        return
      action = body.pop("action", None)
      if not isinstance(action, str):
        self._json({"error": "body needs an action, e.g. {\"action\": \"describe\"}"}, 400)
        return
      try:
        result = runtime.call_node(name, lambda node, ctx: node.calibrate(action, body, ctx))
      except ValueError as exc:
        self._json({"error": str(exc)}, 400)
      except TimeoutError as exc:
        self._json({"error": str(exc)}, 504)
      else:
        self._json(result)

    def _save_calibration(self) -> None:
      if config_path is None:
        self._json({"error": "this session has no robot.yaml to save calibration.yaml beside"}, 409)
        return
      from robot_core.overlay import write_calibration

      patches: dict[str, Any] = {}
      try:
        for name in self._calibratable():
          patch = runtime.call_node(name, lambda node, ctx: node.calibration())
          if patch:
            patches[name] = {"params": patch}
      except TimeoutError as exc:
        self._json({"error": str(exc)}, 504)
        return
      if not patches:
        self._json({"ok": True, "saved": {}, "message": "nothing has been calibrated yet"})
        return
      path = write_calibration(config_path, patches)
      self._json({"ok": True, "saved": patches, "message": f"saved to {path.name}; it applies every time the robot starts"})

    def _camera(self, node_name: str, kind: str) -> None:
      buffer = getattr(runtime.nodes.get(node_name), "frame_buffer", None)
      if buffer is None:
        self._json({"error": f"no camera node named {node_name!r}"}, 404)
        return
      if kind == "/latest":
        frame = buffer.latest() or buffer.wait_newer(0, 2.0)
        if frame is None:
          self._json({"error": "no frame yet"}, 503)
        else:
          self._send(200, frame.data, frame.content_type)
        return
      self.send_response(200)
      self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={_BOUNDARY}")
      self.send_header("Cache-Control", "no-store")
      self.end_headers()
      seq = 0
      try:
        while not server.stopping.is_set():
          frame = buffer.wait_newer(seq, 1.0)
          if frame is None:
            continue
          seq = frame.seq
          head = f"--{_BOUNDARY}\r\nContent-Type: {frame.content_type}\r\nContent-Length: {len(frame.data)}\r\n\r\n"
          self.wfile.write(head.encode("ascii") + frame.data + b"\r\n")
          self.wfile.flush()
      except (BrokenPipeError, ConnectionResetError):
        return

    def do_POST(self) -> None:  # noqa: N802
      path = urlparse(self.path).path
      body = self._body()
      if body is None:
        self._json({"error": "body must be a JSON object under 64 KiB"}, 400)
        return
      if path == "/estop":
        runtime.request_estop(str(body.get("reason") or "operator request"), source="http")
        self._json({"ok": True, "estop": True})
        return
      calibration = _CALIBRATION_PATH.match(path)
      if path not in ("/estop/clear", "/publish") and calibration is None:
        self._json({"error": "not found"}, 404)
        return
      if not self._authorised():
        self._json({"error": "control is disabled or the token is missing/invalid"}, 403)
        return
      if path == "/calibration/save":
        self._save_calibration()
        return
      if calibration is not None:
        self._calibrate(unquote(calibration.group(1)), body)
        return
      if path == "/estop/clear":
        runtime.request_estop_clear(source="http")
        self._json({"ok": True})
        return
      topic, schema, payload = body.get("topic"), body.get("schema", "External"), body.get("payload", {})
      if topic not in topics:
        self._json({"error": f"topic not allowed; control topics: {list(topics)}"}, 403)
        return
      if not isinstance(schema, str) or not isinstance(payload, dict):
        self._json({"error": "schema must be a string and payload an object"}, 400)
        return
      runtime.publish_external(topic, schema, payload, source="http")
      self._json({"ok": True})

  server = LiveHTTPServer((host, port), Handler)
  Thread(target=server.serve_forever, name="nervlynx-http", daemon=True).start()
  return server


_DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NervLynx live</title>
<style>
:root{color-scheme:dark;--bg:#0e1116;--panel:#161b22;--line:#2a313c;--text:#e6edf3;--dim:#8b949e;--ok:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}body{margin:0;font:14px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--text)}
header{display:flex;gap:12px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:1}
h1{font-size:16px;margin:0}.pill{padding:2px 10px;border-radius:999px;font-weight:600;font-size:12px;text-transform:uppercase}
.ok{background:#12351d;color:var(--ok)}.degraded{background:#3a2d0b;color:var(--warn)}.estop,.fault,.offline{background:#3d1214;color:var(--bad)}
.spacer{flex:1}button{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--text);padding:6px 12px;border-radius:6px;cursor:pointer;white-space:nowrap}
button.stop{background:var(--bad);border-color:var(--bad);color:#fff;font-weight:700;padding:8px 20px}
main{padding:16px;display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(min(440px,100%),1fr))}
section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto}
section.wide{grid-column:1/-1}
h2{font-size:12px;margin:0 0 8px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--dim);font-weight:500}
.n{text-align:right}.bad{color:var(--bad)}.dim{color:var(--dim)}.cards{display:flex;gap:20px;flex-wrap:wrap}.cards b{display:block;font-size:20px}
.pad{display:grid;grid-template-columns:repeat(3,64px);gap:6px;margin:8px 0}.pad button{height:48px;touch-action:none}.pad .hot{background:#1f6feb}
td.last{max-width:640px;overflow:hidden;text-overflow:ellipsis;font:12px ui-monospace,Menlo,monospace;color:var(--dim)}
.controls{display:flex;gap:24px;align-items:center;flex-wrap:wrap}
#stick{width:160px;height:160px;border-radius:50%;border:1px solid var(--line);background:#0b0f14;position:relative;touch-action:none;user-select:none}
#knob{width:60px;height:60px;border-radius:50%;background:#1f6feb;position:absolute;left:50px;top:50px;pointer-events:none}
#camgrid{display:flex;gap:12px;flex-wrap:wrap}#camgrid figure{margin:0}#camgrid img{max-width:100%;width:480px;border-radius:6px;background:#000;display:block}
#worldc{width:100%;max-width:640px;background:#0b0f14;border-radius:6px;display:block}
h3{font-size:13px;margin:14px 0 6px}.warn{color:var(--warn)}.calnode+.calnode{border-top:1px solid var(--line);margin-top:10px}
#calibbody p{margin:6px 0;line-height:2.4}#calibbody input[type=range]{width:220px;max-width:100%;vertical-align:middle}
</style></head><body>
<header><h1 id="name">NervLynx</h1><span id="status" class="pill">...</span><span id="uptime" class="dim"></span><span class="spacer"></span>
<button id="clear" hidden>Clear e-stop</button><button class="stop" id="estop">E-STOP</button></header>
<main>
<section><h2>Runtime</h2><div class="cards" id="cards"></div><p id="estopinfo" class="bad"></p></section>
<section id="drive" hidden><h2>Drive &mdash; drag the stick, or hold W A S D / arrows</h2>
<div class="controls"><div id="stick" role="application" aria-label="Drive joystick" tabindex="0"><div id="knob"></div></div>
<div class="pad"><span></span><button data-k="w">&#9650;</button><span></span><button data-k="a">&#9664;</button><button data-k="s">&#9660;</button><button data-k="d">&#9654;</button></div></div>
<label>Speed <input id="speed" type="range" min="0.1" max="1" step="0.05" value="0.5"> <span id="speedv">0.50</span></label>
<p class="dim">Commands stream at 10 Hz while you hold; let go and the drive deadman stops the motors.</p></section>
<section id="world" hidden><h2>Simulation</h2><canvas id="worldc" width="640" height="480"></canvas><p id="worldinfo" class="dim"></p></section>
<section id="cameras" class="wide" hidden><h2>Cameras</h2><div id="camgrid"></div></section>
<section id="calib" class="wide" hidden><h2>Calibrate</h2>
<p class="dim">Changes apply immediately. Save writes calibration.yaml beside robot.yaml so they apply every time this robot starts.</p>
<div id="calibbody"></div><p><button id="calibsave" hidden>Save calibration</button> <span id="calibmsg" class="dim"></span></p></section>
<section class="wide"><h2>Nodes</h2><table id="nodes"></table></section>
<section class="wide"><h2>Topics</h2><table id="topics"></table></section>
<section class="wide"><h2>Recent faults</h2><table id="faults"></table></section>
</main>
<script>
const CFG = __CONFIG__;
const token = new URLSearchParams(location.search).get('token') || '';
const headers = Object.assign({'Content-Type': 'application/json'}, token ? {'X-NervLynx-Token': token} : {});
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const num = (v, d = 2) => v === null || v === undefined ? '&ndash;' : Number(v).toFixed(d);
const post = (path, body) => fetch(path, {method: 'POST', headers, body: JSON.stringify(body || {})});
const table = (el, head, rows) => { el.innerHTML = '<tr>' + head.map(h => '<th>' + h + '</th>').join('') + '</tr>' + rows.join(''); };
$('estop').onclick = () => post('/estop', {reason: 'dashboard button'});
$('clear').onclick = () => post('/estop/clear');
async function refresh() {
  let s;
  try { s = await (await fetch('/stats', {cache: 'no-store'})).json(); }
  catch (e) { $('status').textContent = 'offline'; $('status').className = 'pill offline'; return; }
  const h = s.health;
  $('name').textContent = s.name;
  $('status').textContent = h.status;
  $('status').className = 'pill ' + h.status;
  $('uptime').textContent = 'up ' + num(h.uptime_s, 1) + ' s \u00b7 ' + s.clock + ' clock';
  $('clear').hidden = !(CFG.allow_control && s.estop.engaged);
  $('estopinfo').textContent = s.estop.engaged ? 'E-stop latched by ' + s.estop.source + ': ' + s.estop.reason : '';
  const m = s.messages, x = s.executor;
  $('cards').innerHTML = [['published', m.published], ['delivered', m.delivered], ['dropped', m.dropped],
    ['queue', x.queue_depth], ['steps', x.steps], ['e-stops', s.estop.events], ['stalls', x.stalls]]
    .map(([k, v]) => '<div><span class="dim">' + k + '</span><b>' + v + '</b></div>').join('');
  table($('nodes'), ['node', 'Hz', 'ticks', 'handled', 'errors', 'tick p95 ms', 'late p95 ms', 'handler p95 ms', 'state', 'status'],
    Object.entries(s.nodes).map(([n, v]) => '<tr><td>' + esc(n) + (v.critical ? ' <span class="dim">(critical)</span>' : '') +
      '</td><td class="n">' + (v.rate_hz ?? '') + '</td><td class="n">' + v.ticks + '</td><td class="n">' + v.handled +
      '</td><td class="n' + (v.errors ? ' bad' : '') + '">' + v.errors + '</td><td class="n">' + num(v.tick_ms && v.tick_ms.p95, 3) +
      '</td><td class="n">' + num(v.lateness_ms && v.lateness_ms.p95, 3) + '</td><td class="n">' + num(v.handler_ms.p95, 3) +
      '</td><td class="' + (v.stale || v.breaker_open ? 'bad' : 'dim') + '">' + (v.stale ? 'STALE' : v.breaker_open ? 'BREAKER OPEN' : 'ok') +
      '</td><td class="last" title="' + esc(v.last_error || '') + '">' + esc(brief(v)) + '</td></tr>'));
  showCameras(s.nodes);
  drawWorld(s.nodes);
  table($('topics'), ['topic', 'count', 'Hz', 'latency p50 ms', 'p95 ms', 'last'],
    Object.entries(s.topics).map(([t, v]) => '<tr><td>' + esc(t) + '</td><td class="n">' + v.count + '</td><td class="n">' + num(v.rate_hz, 1) +
      '</td><td class="n">' + num(v.latency_ms.p50, 3) + '</td><td class="n">' + num(v.latency_ms.p95, 3) +
      '</td><td class="last" title="' + esc(JSON.stringify(v.last)) + '">' + esc(JSON.stringify(v.last)) + '</td></tr>'));
  table($('faults'), ['t (s)', 'severity', 'kind', 'message'], s.faults.slice().reverse().slice(0, 12).map(f =>
    '<tr><td class="n">' + num(f.t_s, 2) + '</td><td class="' + (['critical', 'error'].includes(f.severity) ? 'bad' : 'dim') + '">' +
    esc(f.severity) + '</td><td>' + esc(f.kind) + '</td><td>' + esc(f.message) + '</td></tr>'));
}
function brief(v) {
  const st = Object.assign({}, v.status || {});
  delete st.world; delete st.sensors;
  if (st.waiting_for) return 'waiting for ' + st.waiting_for.join(', ');
  if (st.stale_inputs) return 'paused: stale ' + st.stale_inputs.join(', ');
  return Object.keys(st).length ? JSON.stringify(st) : '';
}
const cams = {};
function showCameras(nodes) {
  for (const [n, v] of Object.entries(nodes)) {
    const st = v.status || {};
    if (!st.camera) continue;
    if (!cams[n]) {
      const fig = document.createElement('figure');
      fig.innerHTML = '<img alt="camera ' + esc(n) + '" src="/camera/' + encodeURIComponent(n) + '.mjpg"><figcaption class="dim"></figcaption>';
      $('camgrid').appendChild(fig); cams[n] = fig; $('cameras').hidden = false;
    }
    cams[n].querySelector('figcaption').textContent = n + ' \u00b7 ' + st.source + ' \u00b7 ' + st.size.join('x') + ' \u00b7 ' + num(st.fps, 1) + ' fps';
  }
}
const trail = [];
function drawWorld(nodes) {
  const sim = Object.values(nodes).map(v => v.status || {}).find(st => st.world);
  if (!sim) return;
  $('world').hidden = false;
  const c = $('worldc'), g = c.getContext('2d'), w = sim.world;
  const k = Math.min(c.width / w.width_m, c.height / w.height_m);
  const X = x => x * k, Y = y => (w.height_m - y) * k;
  g.clearRect(0, 0, c.width, c.height);
  g.strokeStyle = '#8b949e'; g.lineWidth = 2; g.strokeRect(0, 0, X(w.width_m), w.height_m * k);
  g.fillStyle = '#3a4250';
  for (const o of w.obstacles) {
    g.beginPath();
    if (o.circle) { g.arc(X(o.circle[0]), Y(o.circle[1]), o.circle[2] * k, 0, 2 * Math.PI); }
    else { const [x0, y0, x1, y1] = o.box; g.rect(X(x0), Y(y1), (x1 - x0) * k, (y1 - y0) * k); }
    g.fill();
  }
  const last = trail[trail.length - 1];
  if (!last || last[0] !== sim.x_m || last[1] !== sim.y_m) { trail.push([sim.x_m, sim.y_m]); if (trail.length > 400) trail.shift(); }
  g.strokeStyle = '#1f6feb55'; g.lineWidth = 2; g.beginPath();
  trail.forEach(([x, y], i) => i ? g.lineTo(X(x), Y(y)) : g.moveTo(X(x), Y(y))); g.stroke();
  const h = sim.heading_deg * Math.PI / 180, r = sim.robot_radius_m;
  for (const sn of sim.sensors || []) {
    const a = h + sn.angle_deg * Math.PI / 180, half = Math.max(sn.beam_deg, 2) * Math.PI / 360;
    const d = (sim.ranges || {})[sn.name] ?? sn.max_range_m;
    const ox = sim.x_m + r * Math.cos(a), oy = sim.y_m + r * Math.sin(a);
    g.fillStyle = d < sn.max_range_m ? '#f8514944' : '#3fb95033';
    g.beginPath(); g.moveTo(X(ox), Y(oy)); g.arc(X(ox), Y(oy), d * k, -(a + half), -(a - half)); g.closePath(); g.fill();
  }
  g.fillStyle = sim.bumped ? '#f85149' : '#3fb950';
  g.beginPath(); g.arc(X(sim.x_m), Y(sim.y_m), r * k, 0, 2 * Math.PI); g.fill();
  g.strokeStyle = '#0e1116'; g.lineWidth = 3; g.beginPath(); g.moveTo(X(sim.x_m), Y(sim.y_m));
  g.lineTo(X(sim.x_m + r * Math.cos(h)), Y(sim.y_m + r * Math.sin(h))); g.stroke();
  $('worldinfo').textContent = 'collisions ' + sim.collisions + (sim.bumped ? ' (touching ' + sim.bumped + ')' : '') +
    ' \u00b7 driven ' + num(sim.distance_m, 2) + ' m \u00b7 speed ' + num(sim.speed_mps, 2) + ' m/s';
}
setInterval(refresh, 500); refresh();
if (CFG.allow_control && CFG.drive_topic) {
  $('drive').hidden = false;
  const held = new Set();
  const dirs = {w: [1, 1], s: [-1, -1], a: [-1, 1], d: [1, -1], ArrowUp: [1, 1], ArrowDown: [-1, -1], ArrowLeft: [-1, 1], ArrowRight: [1, -1]};
  const speed = () => Number($('speed').value);
  $('speed').oninput = () => { $('speedv').textContent = speed().toFixed(2); };
  const send = () => {
    let l = 0, r = 0;
    for (const k of held) { l += dirs[k][0]; r += dirs[k][1]; }
    const peak = Math.max(Math.abs(l), Math.abs(r), 1);
    post('/publish', {topic: CFG.drive_topic, schema: 'DriveCommand', payload: {left: speed() * l / peak, right: speed() * r / peak}});
  };
  const press = k => { if (!dirs[k] || held.has(k)) return; held.add(k); mark(); send(); };
  const release = k => { if (!held.delete(k)) return; mark(); send(); };
  const mark = () => document.querySelectorAll('.pad button').forEach(b => b.classList.toggle('hot', held.has(b.dataset.k)));
  addEventListener('keydown', e => { if (e.target.tagName !== 'INPUT' && dirs[e.key]) { e.preventDefault(); press(e.key); } });
  addEventListener('keyup', e => release(e.key));
  addEventListener('blur', () => { [...held].forEach(release); });
  document.querySelectorAll('.pad button').forEach(b => {
    b.onpointerdown = () => press(b.dataset.k);
    b.onpointerup = b.onpointerleave = () => release(b.dataset.k);
  });
  setInterval(() => { if (held.size) send(); }, 100);
  const stick = $('stick'), knob = $('knob');
  let stickCmd = null;
  const sendStick = () => post('/publish', {topic: CFG.drive_topic, schema: 'DriveCommand', payload: stickCmd});
  const moveStick = e => {
    const b = stick.getBoundingClientRect(), R = b.width / 2;
    let dx = (e.clientX - b.left - R) / R, dy = (e.clientY - b.top - R) / R;
    const m = Math.hypot(dx, dy); if (m > 1) { dx /= m; dy /= m; }
    knob.style.left = (50 + dx * 50) + 'px'; knob.style.top = (50 + dy * 50) + 'px';
    stickCmd = {linear: -dy * speed(), angular: -dx * speed()};
  };
  stick.onpointerdown = e => { moveStick(e); sendStick(); try { stick.setPointerCapture(e.pointerId); } catch (_) {} };
  stick.onpointermove = e => { if (stickCmd) moveStick(e); };
  stick.onpointerup = stick.onpointercancel = () => {
    if (!stickCmd) return;
    stickCmd = {linear: 0, angular: 0}; sendStick(); stickCmd = null;
    knob.style.left = knob.style.top = '50px';
  };
  setInterval(() => { if (stickCmd) sendStick(); }, 100);
}
const panels = {}, probes = {};
const calib = (node, action, args) => post('/calibration/' + encodeURIComponent(node), Object.assign({action}, args || {}))
  .then(async r => { const out = await r.json(); if (!r.ok) throw new Error(out.error || r.statusText); return out; });
const say = (text, bad) => { $('calibmsg').textContent = text || ''; $('calibmsg').className = bad ? 'bad' : 'dim'; };
const btn = (node, action, label, args) => '<button data-node="' + esc(node) + '" data-action="' + action + '" data-args="' +
  esc(JSON.stringify(args || {})) + '">' + label + '</button>';
function drivePanel(n, d) {
  const probe = probes[n] || d.tuning.min_duty;
  const rows = ['left', 'right'].flatMap(side => d.sides[side].map(m => '<p>' + side + ' side &middot; <b>' + esc(m.name) + '</b> ' +
    btn(n, 'spin', 'Spin forward', {motor: m.name}) + ' turned backward? ' +
    btn(n, 'invert', m.invert ? 'Undo invert' : 'Invert it', {motor: m.name, value: !m.invert}) + ' <span class="dim">' +
    (m.invert ? 'inverted' : 'as wired') + '</span></p>'));
  return '<h3>' + esc(n) + ': motors</h3><p class="warn">Put the robot on a box so its wheels spin freely. Each test runs for about a second.</p>' +
    rows.join('') + '<h3>Driving</h3><p>Wheels on the ground, clear space around it: ' +
    btn(n, 'test', 'Drive forward', {move: 'forward'}) + ' ' + btn(n, 'test', 'Turn left', {move: 'left'}) +
    ' &middot; if "turn left" turned it right: ' + btn(n, 'swap_sides', d.swap_sides ? 'Undo swap' : 'Swap sides', {value: !d.swap_sides}) + '</p>' +
    '<h3>Smallest power that moves it</h3><p>' + btn(n, 'probe', 'Try ' + probe.toFixed(2), {duty: probe}) + ' ' +
    btn(n, 'tuning', 'It moved: use ' + probe.toFixed(2), {min_duty: probe}) + ' <span class="dim">now ' + d.tuning.min_duty.toFixed(2) + '</span></p>' +
    (d.savable ? '' : '<p class="bad">Give every motor a name: in robot.yaml so its calibration can be saved.</p>');
}
function imuPanel(n, d) {
  return '<h3>' + esc(n) + ': how the IMU is mounted</h3><p>1. Set the robot flat and still, then ' + btn(n, 'flat', 'Capture flat') +
    '</p><p>2. Lift the front about 30&deg; and hold it still, then ' + btn(n, 'nose_up', 'Capture nose-up') +
    '</p><p class="dim">axes now: forward ' + esc(d.axes[0]) + ', left ' + esc(d.axes[1]) + ', up ' + esc(d.axes[2]) + '</p>';
}
function servoPanel(n, d) {
  return '<h3>' + esc(n) + ': servo travel</h3><p class="dim">Slide slowly towards each end and stop just before the servo buzzes or binds.</p>' +
    d.servos.map(s => '<p>' + esc(s.name) + ' (channel ' + s.channel + ') <input type="range" min="500" max="2500" step="10" value="' +
      (s.us ?? 1500) + '" data-node="' + esc(n) + '" data-servo="' + esc(s.name) + '"> <span>' + Math.round(s.us ?? 1500) + ' us</span> ' +
      btn(n, 'set_limit', 'Set as min', {servo: s.name, which: 'min'}) + ' ' + btn(n, 'set_limit', 'Set as max', {servo: s.name, which: 'max'}) + ' ' +
      btn(n, 'set_limit', 'Set as home', {servo: s.name, which: 'home'}) + ' <span class="dim">' + s.min_us + '&ndash;' + s.max_us + ' us, home ' +
      s.home_deg + '&deg;</span></p>').join('') + '<p>' + btn(n, 'home', 'Go home') + '</p>';
}
function showPanel(n, d) {
  if (!panels[n]) { panels[n] = document.createElement('div'); panels[n].className = 'calnode'; $('calibbody').appendChild(panels[n]); }
  panels[n].innerHTML = d.error ? '<h3>' + esc(n) + '</h3><p class="bad">' + esc(d.error) + '</p>' :
    d.kind === 'drive' ? drivePanel(n, d) : d.kind === 'imu' ? imuPanel(n, d) : d.kind === 'servos' ? servoPanel(n, d) : '';
}
async function loadCalibration() {
  let data;
  try { data = await (await fetch('/calibration' + location.search, {cache: 'no-store'})).json(); } catch (e) { return; }
  if (!Object.keys(data.nodes || {}).length) return;
  $('calib').hidden = false; $('calibsave').hidden = !data.can_save;
  Object.entries(data.nodes).forEach(([n, d]) => showPanel(n, d));
}
$('calibbody').addEventListener('click', async e => {
  const b = e.target.closest('button[data-action]');
  if (!b) return;
  const node = b.dataset.node, args = JSON.parse(b.dataset.args);
  let action = b.dataset.action;
  if (action === 'probe') { action = 'test'; Object.assign(args, {move: 'forward', seconds: 0.8}); }
  try {
    const d = await calib(node, action, args);
    if (b.dataset.action === 'probe') probes[node] = Math.min(args.duty + 0.03, d.max_speed);
    if (action === 'tuning') delete probes[node];
    showPanel(node, d); say(d.message || (d.testing ? 'running: ' + d.testing : 'done'));
  } catch (err) { say(err.message, true); }
});
const jog = s => {
  if (s.busy) { s.again = true; return; }
  s.busy = true;
  calib(s.dataset.node, 'jog', {servo: s.dataset.servo, us: Number(s.value)}).catch(err => say(err.message, true))
    .finally(() => { s.busy = false; if (s.again) { s.again = false; jog(s); } });
};
$('calibbody').addEventListener('input', e => {
  const s = e.target.closest('input[data-servo]');
  if (s) { s.nextElementSibling.textContent = s.value + ' us'; jog(s); }
});
$('calibsave').onclick = async () => {
  try { const r = await post('/calibration/save'); const out = await r.json(); say(out.message || out.error, !r.ok); }
  catch (err) { say(err.message, true); }
};
if (CFG.allow_control) loadCalibration();
</script></body></html>
"""
