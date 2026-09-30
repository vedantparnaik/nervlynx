"""Device mesh: one robot made of several computers (a Pi, an Orin, a laptop) running one graph.

Each computer runs `nervlynx run --device <name>` with the same robot.yaml and gets only
the nodes placed on it. A `mesh` bridge node forwards exactly the topics that cross
devices, so a camera on the Pi can feed a detector on the Orin and the Orin's commands
can reach the Pi's motors:

  devices:
    rover: {host: pi@rover.local}          # the first device runs nodes without placement
    orin: {host: nvidia@orin.local}
  mesh:
    transport: udp                         # or zenoh (pip install "nervlynx[mesh]")
  nodes:
    - {plugin: skid_steer_drive, ...}      # on rover
    - {plugin: follow, placement: orin}

The e-stop is robot-wide: engaging it on any device engages it on every device, and a
device keeps re-engaging while any peer reports it is still latched. Lost links are
covered by the usual deadman and stale-input rules, because forwarded topics stop.

Messages are JSON envelopes. With a shared secret in `NERVLYNX_MESH_KEY` (see
`mesh.key_env`) every message is HMAC-signed, unsigned or replayed ones are dropped, and
senders whose clocks are off by more than `max_skew_s` are rejected. Robots that share a
network need different `mesh.robot` names; messages for other robots are ignored.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from robot_core.live import ESTOP_TOPIC, LiveNode, LiveRuntime, NodeContext, Output
from robot_core.runtime import RuntimeMessage

PROTOCOL_VERSION = 1
TRANSPORTS = ("udp", "zenoh")
DEFAULT_GROUP = "239.255.77.1"
DEFAULT_PORT = 7447
DEFAULT_KEY_ENV = "NERVLYNX_MESH_KEY"
MAX_DATAGRAM = 60_000
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_MESH_KEYS = {"transport", "robot", "group", "port", "peers", "interface", "connect", "listen", "share", "frames", "frame_fps", "key_env", "max_skew_s"}
_DEVICE_KEYS = {"host", "hostname", "description"}
_ESTOP_GRACE_NS = 1_500_000_000


class MeshError(ValueError):
  pass


# ---------------------------------------------------------------------------- config


def devices_of(cfg: dict[str, Any]) -> list[str]:
  devices = cfg.get("devices")
  return list(devices) if isinstance(devices, dict) else []


def placement_of(node_cfg: dict[str, Any], devices: list[str]) -> str | None:
  return node_cfg.get("placement") or (devices[0] if devices else None)


def validate_mesh_config(cfg: dict[str, Any]) -> list[str]:
  """Problems with `devices`, `mesh`, and node `placement`."""
  issues: list[str] = []
  raw_devices = cfg.get("devices")
  if raw_devices is not None:
    if not isinstance(raw_devices, dict) or not raw_devices:
      issues.append("devices must map device names to settings, e.g. devices: {rover: {host: pi@rover.local}}")
      raw_devices = {}
    for name, spec in raw_devices.items():
      if not isinstance(name, str) or not _NAME.match(name):
        issues.append(f"devices: {name!r} is not a usable device name (letters, digits, - _ .)")
        continue
      if spec is None:
        continue
      if not isinstance(spec, dict):
        issues.append(f"devices.{name} must be a mapping (host, hostname, description)")
        continue
      for key in sorted(set(spec) - _DEVICE_KEYS):
        issues.append(f"devices.{name}: unknown key {key}")
      for key in ("host", "hostname", "description"):
        if key in spec and not isinstance(spec[key], str):
          issues.append(f"devices.{name}.{key} must be a string")
  devices = [d for d in (raw_devices or {}) if isinstance(d, str)]
  used: set[str] = set()
  for idx, node in enumerate(cfg.get("nodes") if isinstance(cfg.get("nodes"), list) else []):
    if not isinstance(node, dict):
      continue
    placement = node.get("placement")
    if placement is not None:
      if not devices:
        issues.append(f"nodes[{idx}].placement needs a devices: section listing {placement!r}")
      elif placement not in devices:
        issues.append(f"nodes[{idx}].placement {placement!r} is not one of the devices: {', '.join(devices)}")
    target = placement_of(node, devices)
    if target is not None:
      used.add(target)
  mesh = cfg.get("mesh")
  if len(used) > 1 and mesh is None:
    issues.append(f"nodes run on {', '.join(sorted(used))} but there is no mesh: section to connect them")
  if mesh is None:
    return issues
  if not isinstance(mesh, dict):
    return issues + ["mesh must be a mapping, e.g. mesh: {transport: udp}"]
  for key in sorted(set(mesh) - _MESH_KEYS):
    issues.append(f"unknown mesh key: {key}")
  transport = mesh.get("transport", "udp")
  if transport not in TRANSPORTS:
    issues.append(f"mesh.transport must be one of {', '.join(TRANSPORTS)}")
  if "robot" in mesh and (not isinstance(mesh["robot"], str) or not _NAME.match(mesh["robot"])):
    issues.append("mesh.robot must be a simple name (letters, digits, - _ .)")
  port = mesh.get("port", DEFAULT_PORT)
  if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
    issues.append("mesh.port must be 1..65535")
  if "group" in mesh:
    try:
      if not ipaddress.ip_address(str(mesh["group"])).is_multicast:
        raise ValueError
    except ValueError:
      issues.append("mesh.group must be an IPv4 multicast address such as 239.255.77.1")
  for key in ("peers", "connect", "listen", "share", "frames"):
    value = mesh.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
      issues.append(f"mesh.{key} must be a list of strings")
  if "interface" in mesh:
    try:
      ipaddress.IPv4Address(str(mesh["interface"]))
    except ValueError:
      issues.append("mesh.interface must be the IPv4 address of this computer's network interface, e.g. 192.168.1.20")
  if transport == "udp":
    for key in ("connect", "listen"):
      if mesh.get(key):
        issues.append(f"mesh.{key} is for transport: zenoh; UDP uses peers: (or multicast by default)")
    if mesh.get("frames"):
      issues.append("mesh.frames needs transport: zenoh (camera frames are too big for UDP datagrams)")
  elif mesh.get("peers"):
    issues.append("mesh.peers is for transport: udp; zenoh uses connect: and listen:")
  for key in ("frame_fps", "max_skew_s"):
    value = mesh.get(key)
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
      issues.append(f"mesh.{key} must be a positive number")
  if "key_env" in mesh and not isinstance(mesh["key_env"], str):
    issues.append("mesh.key_env must be the name of an environment variable")
  return issues


def detect_device(cfg: dict[str, Any], hostname: str | None = None) -> str:
  """This machine's device name, matched on `devices.<name>.hostname` (default the name)."""
  host = (hostname or socket.gethostname()).split(".")[0].lower()
  devices = cfg.get("devices") or {}
  matches = [name for name, spec in devices.items() if ((spec or {}).get("hostname") or name).split(".")[0].lower() == host]
  if len(matches) == 1:
    return matches[0]
  names = ", ".join(devices)
  raise MeshError(f"this computer ({host}) is not one of the devices ({names}); pass --device NAME or set devices.<name>.hostname: {host}")


@dataclass(frozen=True)
class MeshPlan:
  robot: str
  device: str
  nodes: list[dict[str, Any]]
  out_topics: frozenset[str]
  in_topics: frozenset[str]
  frames_out: tuple[str, ...] = ()
  frames_in: tuple[str, ...] = ()
  peers: tuple[str, ...] = field(default=())


def _camera_name(node_cfg: dict[str, Any]) -> str | None:
  if node_cfg.get("plugin") != "camera":
    return None
  return str((node_cfg.get("params") or {}).get("name", "front"))


def plan_mesh(cfg: dict[str, Any], device: str, topics_of: Callable[[dict[str, Any]], Iterable[str]]) -> MeshPlan:
  """Which nodes run on `device`, and which topics and frames cross to other devices.

  `topics_of(node_cfg)` returns a node's input topics (see live_config).
  """
  devices = devices_of(cfg)
  if device not in devices:
    raise MeshError(f"--device {device!r} is not one of the devices: {', '.join(devices)}")
  local, remote = [], []
  for node in cfg.get("nodes") or []:
    (local if placement_of(node, devices) == device else remote).append(node)
  mesh = cfg.get("mesh") or {}
  local_inputs = {topic for node in local for topic in topics_of(node)}
  remote_inputs = {topic for node in remote for topic in topics_of(node)}
  shared_frames = list(mesh.get("frames") or [])
  local_cameras = {_camera_name(node) for node in local} - {None}
  return MeshPlan(
    robot=str(mesh.get("robot") or cfg.get("name") or "robot"),
    device=device,
    nodes=local,
    out_topics=frozenset(remote_inputs | set(mesh.get("share") or [])),
    in_topics=frozenset(local_inputs),
    frames_out=tuple(name for name in shared_frames if name in local_cameras),
    frames_in=tuple(name for name in shared_frames if name not in local_cameras),
    peers=tuple(d for d in devices if d != device),
  )


# ---------------------------------------------------------------------------- wire format


def encode(envelope: dict[str, Any], key: bytes | None, blob: bytes | None = None) -> bytes:
  """JSON envelope (plus an optional binary blob after a newline), HMAC-prefixed with a key."""
  body = json.dumps(envelope, separators=(",", ":"), default=str).encode("utf-8")
  if blob is not None:
    body += b"\n" + blob
  if key is None:
    return body
  return b"s:" + hmac.new(key, body, hashlib.sha256).hexdigest().encode("ascii") + b":" + body


def decode(wire: bytes, key: bytes | None) -> tuple[dict[str, Any], bytes | None, bool]:
  """(envelope, blob, signed). With a key, anything unsigned or badly signed raises MeshError."""
  signed = wire.startswith(b"s:")
  if signed:
    if len(wire) < 68 or wire[66:67] != b":":
      raise MeshError("malformed signature")
    signature, body = wire[2:66], wire[67:]
    if key is not None and not hmac.compare_digest(signature, hmac.new(key, body, hashlib.sha256).hexdigest().encode("ascii")):
      raise MeshError("bad signature")
  elif key is not None:
    raise MeshError("unsigned message")
  else:
    body = wire
  head, sep, blob = body.partition(b"\n")
  try:
    envelope = json.loads(head)
  except (UnicodeDecodeError, ValueError):
    raise MeshError("not a mesh message") from None
  if not isinstance(envelope, dict) or envelope.get("v") != PROTOCOL_VERSION:
    raise MeshError("unknown mesh protocol version")
  return envelope, (blob if sep else None), signed


# ---------------------------------------------------------------------------- transports


class MeshTransport:
  """Moves opaque messages between devices. `on_receive` may be called from any thread."""

  name = "base"

  def open(self, on_receive: Callable[[bytes], None]) -> None:
    raise NotImplementedError

  def send(self, key: str, data: bytes) -> None:
    raise NotImplementedError

  def close(self) -> None:
    return None


class MemoryBus:
  """Connects MemoryTransports in one process (tests, and single-process demos)."""

  def __init__(self) -> None:
    self.endpoints: list[MemoryTransport] = []
    self.sent = 0


class MemoryTransport(MeshTransport):
  name = "memory"

  def __init__(self, bus: MemoryBus) -> None:
    self.bus = bus
    self._on_receive: Callable[[bytes], None] | None = None

  def open(self, on_receive: Callable[[bytes], None]) -> None:
    self._on_receive = on_receive
    self.bus.endpoints.append(self)

  def send(self, key: str, data: bytes) -> None:
    self.bus.sent += 1
    for endpoint in list(self.bus.endpoints):
      if endpoint is not self and endpoint._on_receive is not None:
        endpoint._on_receive(bytes(data))

  def close(self) -> None:
    if self in self.bus.endpoints:
      self.bus.endpoints.remove(self)
    self._on_receive = None


def _udp_target(peer: str, port: int) -> tuple[str, int]:
  host, sep, peer_port = peer.rpartition(":")
  return (host, int(peer_port)) if sep and peer_port.isdigit() else (peer, port)


class UdpTransport(MeshTransport):
  """UDP multicast on the local network, or unicast to `peers` ("host" or "host:port")
  where multicast is blocked (some Wi-Fi access points drop it). Standard library only."""

  name = "udp"

  def __init__(self, *, group: str = DEFAULT_GROUP, port: int = DEFAULT_PORT, peers: Iterable[str] = (), interface: str | None = None) -> None:
    self.group = group
    self.port = int(port)
    self.peers = tuple(peers)
    self.interface = interface or "0.0.0.0"
    self.oversize = 0
    self.send_errors = 0
    self.last_error: str | None = None
    self._sock: socket.socket | None = None
    self._thread: threading.Thread | None = None
    self._stop = threading.Event()
    self._targets: list[tuple[str, int]] = []

  def open(self, on_receive: Callable[[bytes], None]) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
      sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", self.port))
    if not self.peers:
      membership = struct.pack("4s4s", socket.inet_aton(self.group), socket.inet_aton(self.interface))
      sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
      if self.interface != "0.0.0.0":
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.interface))
      sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
      sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sock.settimeout(0.25)
    self._sock = sock
    self._targets = [_udp_target(peer, self.port) for peer in self.peers] or [(self.group, self.port)]

    def loop() -> None:
      while not self._stop.is_set():
        try:
          data, _ = sock.recvfrom(65535)
        except socket.timeout:
          continue
        except OSError:
          return
        on_receive(data)

    self._thread = threading.Thread(target=loop, name="nervlynx-mesh-udp", daemon=True)
    self._thread.start()

  def send(self, key: str, data: bytes) -> None:
    if self._sock is None:
      return
    if len(data) > MAX_DATAGRAM:
      self.oversize += 1
      return
    for target in self._targets:
      try:
        self._sock.sendto(data, target)
      except OSError as exc:
        self.send_errors += 1
        self.last_error = f"sending to {target[0]}:{target[1]} failed: {exc}"

  def close(self) -> None:
    self._stop.set()
    if self._thread is not None:
      self._thread.join(timeout=1.0)
    if self._sock is not None:
      self._sock.close()
      self._sock = None


def create_transport(mesh: dict[str, Any], *, robot: str) -> MeshTransport:
  transport = mesh.get("transport", "udp")
  if transport == "udp":
    return UdpTransport(
      group=str(mesh.get("group", DEFAULT_GROUP)),
      port=int(mesh.get("port", DEFAULT_PORT)),
      peers=mesh.get("peers") or (),
      interface=mesh.get("interface"),
    )
  if transport == "zenoh":
    from robot_core.mesh_zenoh import ZenohTransport

    return ZenohTransport(robot=robot, connect=mesh.get("connect") or (), listen=mesh.get("listen") or ())
  raise MeshError(f"unknown mesh transport {transport!r}")


def mesh_key(mesh: dict[str, Any], environ: dict[str, str] | None = None) -> bytes | None:
  value = (environ if environ is not None else os.environ).get(str(mesh.get("key_env", DEFAULT_KEY_ENV)), "")
  return value.encode("utf-8") if value else None


# ---------------------------------------------------------------------------- bridge node


class MeshNode(LiveNode):
  """Bridges this device's runtime to the other devices of the same robot."""

  rate_hz = 20.0

  def __init__(
    self,
    runtime: LiveRuntime,
    *,
    plan: MeshPlan,
    transport: MeshTransport,
    key: bytes | None = None,
    frame_fps: float = 10.0,
    max_skew_s: float = 30.0,
    heartbeat_s: float = 0.5,
    peer_timeout_s: float = 2.0,
  ) -> None:
    self.runtime = runtime
    self.plan = plan
    self.transport = transport
    self.key = key
    self.frame_fps = float(frame_fps)
    self.max_skew_s = float(max_skew_s)
    self.heartbeat_ns = int(heartbeat_s * 1e9)
    self.peer_timeout_ns = int(peer_timeout_s * 1e9)
    self.session = secrets.token_hex(4)
    self.tx = self.rx = self.dropped = 0
    self._seq = 0
    self._lock = threading.Lock()
    self._peers: dict[str, dict[str, Any]] = {}
    self._last_seq: dict[tuple[str, str], int] = {}
    self._notes: deque[tuple[str, str]] = deque(maxlen=64)
    self._noted: set[str] = set()
    self._last_hb_ns: int | None = None
    self._cleared_ns: int | None = None
    self._reported_error: str | None = None
    self._stop = threading.Event()
    self._senders: list[threading.Thread] = []
    self._frames_in: dict[str, Any] = {}

  # executor thread --------------------------------------------------------

  def setup(self, ctx: NodeContext) -> None:
    if self.plan.frames_in:
      from robot_core.camera import FrameBuffer, register_frames

      for name in self.plan.frames_in:
        self._frames_in[name] = register_frames(name, FrameBuffer())
    self.transport.open(self._receive)
    for name in self.plan.frames_out:
      sender = threading.Thread(target=self._send_frames, args=(name,), name=f"nervlynx-mesh-frames-{name}", daemon=True)
      sender.start()
      self._senders.append(sender)
    self._heartbeat(ctx)

  def on_local_message(self, msg: RuntimeMessage) -> None:
    """Message listener: forward what other devices need (runs on the executor thread)."""
    topic = msg.envelope.topic
    if topic == ESTOP_TOPIC:
      payload = msg.payload
      if str(payload.get("source", "")).startswith("mesh:"):
        if not payload.get("engaged"):
          self._cleared_ns = self.runtime.clock.monotonic_ns()
        return
      if not payload.get("engaged"):
        self._cleared_ns = self.runtime.clock.monotonic_ns()
      self._send({"k": "estop", "engaged": bool(payload.get("engaged")), "reason": str(payload.get("reason", ""))})
      return
    if topic not in self.plan.out_topics or msg.envelope.source.startswith("mesh:"):
      return
    self._send({"k": "msg", "topic": topic, "schema": msg.envelope.schema, "payload": msg.payload})

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    now = ctx.now_ns
    if self._last_hb_ns is None or now - self._last_hb_ns >= self.heartbeat_ns:
      self._heartbeat(ctx)
    with self._lock:
      peers = {name: dict(info) for name, info in self._peers.items()}
    for name, info in peers.items():
      silent = now - info["last_ns"]
      if info["online"] and silent > self.peer_timeout_ns:
        self._set_peer(name, online=False)
        ctx.fault(f"mesh: lost contact with {name} ({silent / 1e9:.1f} s silent); its topics have stopped", kind="mesh", severity="error")
    while self._notes:
      severity, message = self._notes.popleft()
      ctx.fault(message, kind="mesh", severity=severity)
    error = getattr(self.transport, "last_error", None)
    if error and error != self._reported_error:
      self._reported_error = error
      hint = "; if this network or OS blocks multicast, list the other devices under mesh.peers" if self.transport.name == "udp" else ""
      ctx.fault(f"mesh: {self.transport.name} {error}{hint}", kind="mesh", severity="error")
    return None

  def teardown(self, ctx: NodeContext) -> None:
    self._stop.set()
    for sender in self._senders:
      sender.join(timeout=1.0)
    self.transport.close()
    if self._frames_in:
      from robot_core.camera import unregister_frames

      for name, buffer in self._frames_in.items():
        unregister_frames(name, buffer)

  def status(self) -> dict[str, Any]:
    now = self.runtime.clock.monotonic_ns()
    with self._lock:
      peers = {
        name: {"online": info["online"], "last_seen_s": round((now - info["last_ns"]) / 1e9, 2), "rx": info["rx"], "estop": info.get("estop", False)}
        for name, info in self._peers.items()
      }
    return {
      "mesh": True,
      "device": self.plan.device,
      "robot": self.plan.robot,
      "transport": self.transport.name,
      "signed": self.key is not None,
      "peers": peers,
      "expected_peers": list(self.plan.peers),
      "out_topics": sorted(self.plan.out_topics),
      "in_topics": sorted(self.plan.in_topics),
      "frames_out": list(self.plan.frames_out),
      "frames_in": list(self.plan.frames_in),
      "tx": self.tx,
      "rx": self.rx,
      "dropped": self.dropped,
    }

  # sending ------------------------------------------------------------------

  def _envelope(self, fields: dict[str, Any]) -> dict[str, Any]:
    with self._lock:
      self._seq += 1
      seq = self._seq
    return {"v": PROTOCOL_VERSION, "r": self.plan.robot, "d": self.plan.device, "s": self.session, "n": seq, "t": round(time.time(), 3), **fields}

  def _send(self, fields: dict[str, Any], blob: bytes | None = None) -> None:
    key = fields["k"] + ("/" + fields["topic"] if "topic" in fields else "/" + fields["name"] if "name" in fields else "")
    try:
      wire = encode(self._envelope(fields), self.key, blob)
    except (TypeError, ValueError) as exc:
      self._note("warning", f"mesh: could not send {key}: {exc}")
      return
    try:
      self.transport.send(key, wire)
    except Exception as exc:  # noqa: BLE001 - a transport hiccup must not break the executor
      self._note("warning", f"mesh: {self.transport.name} send failed: {exc}")
      return
    self.tx += 1

  def _heartbeat(self, ctx: NodeContext) -> None:
    self._last_hb_ns = ctx.now_ns
    engaged = self.runtime.estop_engaged
    self._send({"k": "hb", "estop": engaged, "reason": self.runtime.estop_reason if engaged else "", "nodes": sorted(self.runtime.nodes)})

  def _local_frames(self, name: str) -> Any:
    for node in self.runtime.nodes.values():
      if getattr(node, "camera_name", None) == name and getattr(node, "active_source", None) not in (None, "none"):
        return node.frame_buffer
    return None

  def _send_frames(self, name: str) -> None:
    seq, last = 0, 0.0
    gap = 1.0 / self.frame_fps
    while not self._stop.is_set():
      buffer = self._local_frames(name)
      if buffer is None:
        self._stop.wait(0.5)
        continue
      frame = buffer.wait_newer(seq, 0.5)
      if frame is None:
        continue
      seq = frame.seq
      now = time.monotonic()
      if now - last < gap:
        continue
      last = now
      self._send({"k": "frame", "name": name, "type": frame.content_type, "w": frame.width, "h": frame.height, "seq": frame.seq}, frame.data)

  # receiving (transport threads) ---------------------------------------------

  def _note(self, severity: str, message: str) -> None:
    if message not in self._noted:
      self._noted.add(message)
      self._notes.append((severity, message))

  def _set_peer(self, name: str, **changes: Any) -> None:
    with self._lock:
      if name in self._peers:
        self._peers[name].update(changes)

  def _receive(self, wire: bytes) -> None:
    try:
      env, blob, signed = decode(wire, self.key)
    except MeshError as exc:
      self.dropped += 1
      self._note("warning", f"mesh: dropped a message ({exc}); every device needs the same {DEFAULT_KEY_ENV}")
      return
    if env.get("r") != self.plan.robot:
      return
    peer, session = str(env.get("d")), str(env.get("s"))
    if peer == self.plan.device:
      if session != self.session:
        self._note("error", f"mesh: another process is also running as device {peer}; each device needs its own --device name")
      return
    if self.key is None and signed:
      self._note("warning", f"mesh: {peer} signs its messages but this device has no key; set {DEFAULT_KEY_ENV} here too")
    if self.key is not None:
      stamp = env.get("t")
      if not isinstance(stamp, (int, float)) or abs(time.time() - stamp) > self.max_skew_s:
        self.dropped += 1
        self._note("warning", f"mesh: {peer}'s clock is more than {self.max_skew_s:.0f} s off; keep the devices' clocks in sync (NTP)")
        return
      last = self._last_seq.get((peer, session), 0)
      seq = env.get("n")
      if not isinstance(seq, int) or seq <= last:
        self.dropped += 1
        return
      self._last_seq[(peer, session)] = seq
    now = self.runtime.clock.monotonic_ns()
    with self._lock:
      info = self._peers.get(peer)
      rejoined = info is not None and not info["online"]
      if info is None:
        info = self._peers[peer] = {"online": True, "last_ns": now, "rx": 0}
        self._notes.append(("info", f"mesh: {peer} joined"))
      info.update(online=True, last_ns=now)
      info["rx"] += 1
    if rejoined:
      self._notes.append(("info", f"mesh: {peer} is back"))
    self.rx += 1
    kind = env.get("k")
    if kind == "msg":
      topic = env.get("topic")
      if topic in self.plan.in_topics and isinstance(env.get("payload"), dict):
        self.runtime.publish_external(str(topic), str(env.get("schema", "Message")), env["payload"], source=f"mesh:{peer}")
    elif kind == "estop":
      if env.get("engaged"):
        self.runtime.request_estop(f"{env.get('reason') or 'e-stop'} (on {peer})", source=f"mesh:{peer}")
      else:
        self.runtime.request_estop_clear(source=f"mesh:{peer}")
    elif kind == "hb":
      with self._lock:
        info["estop"] = bool(env.get("estop"))
      recently_cleared = self._cleared_ns is not None and now - self._cleared_ns < _ESTOP_GRACE_NS
      if env.get("estop") and not self.runtime.estop_engaged and not recently_cleared:
        self.runtime.request_estop(f"{peer} is still e-stopped: {env.get('reason') or 'no reason given'}", source=f"mesh:{peer}")
    elif kind == "frame" and blob is not None:
      buffer = self._frames_in.get(str(env.get("name")))
      if buffer is not None:
        buffer.put(blob, str(env.get("type", "image/jpeg")), int(env.get("w", 0)), int(env.get("h", 0)))
