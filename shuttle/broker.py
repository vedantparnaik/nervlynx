from __future__ import annotations

import zmq


def run_broker(publisher_address: str, subscriber_address: str) -> None:
  ctx = zmq.Context.instance()
  frontend = ctx.socket(zmq.XSUB)
  backend = ctx.socket(zmq.XPUB)
  frontend.bind(publisher_address)
  backend.bind(subscriber_address)
  try:
    zmq.proxy(frontend, backend)
  finally:
    frontend.close(0)
    backend.close(0)
