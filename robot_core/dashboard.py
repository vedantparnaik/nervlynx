from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from robot_core.metrics import MetricsRegistry
from robot_core.runtime import PipelineRuntime


@dataclass(frozen=True)
class DashboardSnapshot:
  subscriptions: dict[str, int]
  node_heartbeats_count: int
  fault_count: int


def snapshot_runtime(runtime: PipelineRuntime) -> DashboardSnapshot:
  subscriptions = {topic: len(handlers) for topic, handlers in runtime._subscriptions.items()}  # type: ignore[attr-defined]
  return DashboardSnapshot(
    subscriptions=subscriptions,
    node_heartbeats_count=len(runtime.node_heartbeats_ns),
    fault_count=len(runtime.faults),
  )


def serve_dashboard(runtime: PipelineRuntime, metrics: MetricsRegistry, host: str = "0.0.0.0", port: int = 9120) -> HTTPServer:
  class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
      if self.path == "/health":
        body = b'{"status":"ok"}'
      elif self.path == "/graph":
        body = json.dumps(asdict(snapshot_runtime(runtime))).encode("utf-8")
      elif self.path == "/stats":
        body = json.dumps(
          {
            "heartbeats": runtime.node_heartbeats_ns,
            "faults": runtime.faults,
            "metrics": metrics.render_prometheus(),
          }
        ).encode("utf-8")
      else:
        self.send_response(404)
        self.end_headers()
        return
      self.send_response(200)
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
      return

  server = HTTPServer((host, port), Handler)
  thread = Thread(target=server.serve_forever, daemon=True)
  thread.start()
  return server
