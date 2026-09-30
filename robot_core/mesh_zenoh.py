"""Zenoh transport for the device mesh (`mesh: {transport: zenoh}`; pip install "nervlynx[mesh]").

Zenoh routes through peers and routers, crosses subnets, carries camera frames, and has
prebuilt wheels for aarch64 and armv7, so it installs without compiling on a Pi or an
Orin. Every message of robot R is put under the key `nervlynx/R/<kind>/<topic>`; e-stops
and heartbeats are sent at real-time priority without batching. With no `connect:`
endpoints, peers find each other by multicast scouting on the local network.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable

from robot_core.mesh import MeshTransport

_UNSAFE = re.compile(r"[*$?#]")


def _key_part(text: str) -> str:
  return "/".join(chunk or "_" for chunk in _UNSAFE.sub("_", text).strip("/").split("/"))


def _import_zenoh() -> Any:
  try:
    import zenoh  # type: ignore[import-not-found]
  except ImportError as exc:
    raise ModuleNotFoundError('mesh transport zenoh needs eclipse-zenoh: pip install "nervlynx[mesh]"') from exc
  return zenoh


class ZenohTransport(MeshTransport):
  name = "zenoh"

  def __init__(self, *, robot: str, connect: Iterable[str] = (), listen: Iterable[str] = (), prefix: str = "nervlynx", scouting: bool | None = None) -> None:
    self.robot = robot
    self.connect = list(connect)
    self.listen = list(listen)
    self.prefix = prefix
    self.scouting = (not self.connect) if scouting is None else scouting
    self.last_error: str | None = None
    self._session: Any = None
    self._subscriber: Any = None
    self._urgent: dict[str, Any] = {}

  def _key(self, key: str) -> str:
    return f"{self.prefix}/{_key_part(self.robot)}/{_key_part(key)}"

  def open(self, on_receive: Callable[[bytes], None]) -> None:
    zenoh = _import_zenoh()
    zenoh.init_log_from_env_or("error")
    conf = zenoh.Config()
    conf.insert_json5("mode", json.dumps("peer"))
    if self.connect:
      conf.insert_json5("connect/endpoints", json.dumps(self.connect))
    if self.listen:
      conf.insert_json5("listen/endpoints", json.dumps(self.listen))
    conf.insert_json5("scouting/multicast/enabled", json.dumps(self.scouting))
    self._session = zenoh.open(conf)
    self._urgent = {"priority": zenoh.Priority.REAL_TIME, "express": True}

    def handler(sample: Any) -> None:
      payload = sample.payload
      on_receive(payload.to_bytes() if hasattr(payload, "to_bytes") else bytes(payload))

    self._subscriber = self._session.declare_subscriber(f"{self.prefix}/{_key_part(self.robot)}/**", handler)

  def send(self, key: str, data: bytes) -> None:
    if self._session is None:
      return
    urgent = key.startswith(("estop", "hb"))
    try:
      self._session.put(self._key(key), data, **(self._urgent if urgent else {}))
    except Exception as exc:  # noqa: BLE001 - reported by the mesh node
      self.last_error = f"put failed: {exc}"

  def close(self) -> None:
    if self._subscriber is not None:
      try:
        self._subscriber.undeclare()
      except Exception:  # noqa: BLE001 - closing anyway
        pass
      self._subscriber = None
    if self._session is not None:
      self._session.close()
      self._session = None
