"""HTTP control and observability surface for a running `LiveRuntime`.

GET  /           live dashboard (HTML)
GET  /metrics    Prometheus text exposition
GET  /health     status summary
GET  /graph      nodes, topics, and subscriptions
GET  /stats      full runtime snapshot (JSON)
GET  /faults     recent structured faults
POST /estop      latch the e-stop (always allowed)
POST /estop/clear            requires control access
POST /publish {topic, schema, payload}   requires control access and an allowed topic

Control access means the server was started with `allow_control=True` and, if a token
is configured, the request carries it (header `X-NervLynx-Token` or `?token=`).
"""

from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

from robot_core.live import LiveRuntime

_MAX_BODY_BYTES = 64 * 1024


def serve_live(
  runtime: LiveRuntime,
  *,
  host: str = "127.0.0.1",
  port: int = 9120,
  allow_control: bool = False,
  control_topics: Iterable[str] = ("cmd.drive",),
  control_token: str | None = None,
) -> ThreadingHTTPServer:
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
      else:
        self._json({"error": "not found"}, 404)

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
      if path not in ("/estop/clear", "/publish"):
        self._json({"error": "not found"}, 404)
        return
      if not self._authorised():
        self._json({"error": "control is disabled or the token is missing/invalid"}, 403)
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

  server = ThreadingHTTPServer((host, port), Handler)
  server.daemon_threads = True
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
</style></head><body>
<header><h1 id="name">NervLynx</h1><span id="status" class="pill">...</span><span id="uptime" class="dim"></span><span class="spacer"></span>
<button id="clear" hidden>Clear e-stop</button><button class="stop" id="estop">E-STOP</button></header>
<main>
<section><h2>Runtime</h2><div class="cards" id="cards"></div><p id="estopinfo" class="bad"></p></section>
<section id="drive" hidden><h2>Drive &mdash; hold W A S D or arrows</h2>
<div class="pad"><span></span><button data-k="w">&#9650;</button><span></span><button data-k="a">&#9664;</button><button data-k="s">&#9660;</button><button data-k="d">&#9654;</button></div>
<label>Speed <input id="speed" type="range" min="0.1" max="1" step="0.05" value="0.5"> <span id="speedv">0.50</span></label>
<p class="dim">Commands stream at 10 Hz while a key is held; release and the drive deadman stops the motors.</p></section>
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
  table($('nodes'), ['node', 'Hz', 'ticks', 'handled', 'errors', 'tick p95 ms', 'late p95 ms', 'handler p95 ms', 'state'],
    Object.entries(s.nodes).map(([n, v]) => '<tr><td>' + esc(n) + (v.critical ? ' <span class="dim">(critical)</span>' : '') +
      '</td><td class="n">' + (v.rate_hz ?? '') + '</td><td class="n">' + v.ticks + '</td><td class="n">' + v.handled +
      '</td><td class="n' + (v.errors ? ' bad' : '') + '">' + v.errors + '</td><td class="n">' + num(v.tick_ms && v.tick_ms.p95, 3) +
      '</td><td class="n">' + num(v.lateness_ms && v.lateness_ms.p95, 3) + '</td><td class="n">' + num(v.handler_ms.p95, 3) +
      '</td><td class="' + (v.stale || v.breaker_open ? 'bad' : 'dim') + '">' + (v.stale ? 'STALE' : v.breaker_open ? 'BREAKER OPEN' : 'ok') + '</td></tr>'));
  table($('topics'), ['topic', 'count', 'Hz', 'latency p50 ms', 'p95 ms', 'last'],
    Object.entries(s.topics).map(([t, v]) => '<tr><td>' + esc(t) + '</td><td class="n">' + v.count + '</td><td class="n">' + num(v.rate_hz, 1) +
      '</td><td class="n">' + num(v.latency_ms.p50, 3) + '</td><td class="n">' + num(v.latency_ms.p95, 3) +
      '</td><td class="last" title="' + esc(JSON.stringify(v.last)) + '">' + esc(JSON.stringify(v.last)) + '</td></tr>'));
  table($('faults'), ['t (s)', 'severity', 'kind', 'message'], s.faults.slice().reverse().slice(0, 12).map(f =>
    '<tr><td class="n">' + num(f.t_s, 2) + '</td><td class="' + (['critical', 'error'].includes(f.severity) ? 'bad' : 'dim') + '">' +
    esc(f.severity) + '</td><td>' + esc(f.kind) + '</td><td>' + esc(f.message) + '</td></tr>'));
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
}
</script></body></html>
"""
