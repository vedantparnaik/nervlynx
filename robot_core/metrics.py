from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread


class MetricsRegistry:
  def __init__(self) -> None:
    self._counters: dict[str, float] = {}
    self._gauges: dict[str, float] = {}

  def inc(self, name: str, amount: float = 1.0) -> None:
    self._counters[name] = self._counters.get(name, 0.0) + amount

  def set_gauge(self, name: str, value: float) -> None:
    self._gauges[name] = value

  def render_prometheus(self) -> str:
    lines: list[str] = []
    for name, value in sorted(self._counters.items()):
      lines.append(f"# TYPE {name} counter")
      lines.append(f"{name} {value}")
    for name, value in sorted(self._gauges.items()):
      lines.append(f"# TYPE {name} gauge")
      lines.append(f"{name} {value}")
    return "\n".join(lines) + ("\n" if lines else "")


def serve_metrics(registry: MetricsRegistry, host: str = "0.0.0.0", port: int = 9108) -> HTTPServer:
  class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
      if self.path != "/metrics":
        self.send_response(404)
        self.end_headers()
        return
      body = registry.render_prometheus().encode("utf-8")
      self.send_response(200)
      self.send_header("Content-Type", "text/plain; version=0.0.4")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
      return

  server = HTTPServer((host, port), Handler)
  thread = Thread(target=server.serve_forever, daemon=True)
  thread.start()
  return server
