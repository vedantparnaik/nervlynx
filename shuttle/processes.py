from __future__ import annotations

import base64
import json
from pathlib import Path

from shuttle.bus import Publisher, Subscriber, capnp_to_bytes
from shuttle.config import load_config
from shuttle.messages import message_defs
from shuttle.route import load_route, snapshot_from_odom
from shuttle.safety import SafetySupervisor
from shuttle.sensors import ReplaySensorAdapter, SensorAdapter, SyntheticSensorAdapter
from shuttle.timeutil import monotonic_ns, sleep_to_rate


def _sensor_adapter(sensor_mode: str, replay_path: Path | None) -> SensorAdapter:
  cfg = load_config()
  if sensor_mode == "replay":
    if replay_path is None:
      raise ValueError("replay mode requires --replay-path")
    return ReplaySensorAdapter(replay_path)
  return SyntheticSensorAdapter(state_hz=cfg.topics["vehicleState"].hz)


def run_state_estimator(sensor_mode: str = "synthetic", replay_path: Path | None = None) -> None:
  cfg = load_config()
  defs = message_defs()
  pub = Publisher(cfg.publisher_address, source="state_estimator")
  sensors = _sensor_adapter(sensor_mode=sensor_mode, replay_path=replay_path)
  hb_last = monotonic_ns()
  try:
    while True:
      loop_start = monotonic_ns()
      frame = sensors.read()
      m = defs["vehicleState"].cls.new_message()
      m.schemaVersion = 1
      m.speedMps = frame.speed_mps
      m.yawRateRps = frame.yaw_rate_rps
      m.wheelOdomM = frame.wheel_odom_m
      m.latDeg = frame.lat_deg
      m.lonDeg = frame.lon_deg
      m.headingDeg = frame.heading_deg
      pub.send("vehicleState", "VehicleState", capnp_to_bytes(m))

      if monotonic_ns() - hb_last > 1_000_000_000:
        hb = defs["heartbeat"].cls.new_message()
        hb.schemaVersion = 1
        hb.processName = "state_estimator"
        hb.healthy = True
        hb.statusText = "ok"
        pub.send("heartbeat", "Heartbeat", capnp_to_bytes(hb))
        hb_last = monotonic_ns()
      sleep_to_rate(cfg.topics["vehicleState"].hz, loop_start)
  finally:
    pub.close()


def run_planner() -> None:
  cfg = load_config()
  defs = message_defs()
  sub = Subscriber(cfg.subscriber_address, topics=["vehicleState", "routeProgress", "safetyState"])
  pub = Publisher(cfg.publisher_address, source="planner")
  hb_last = monotonic_ns()
  target_from_route = 2.0
  safety_mode = "normal"
  try:
    while True:
      loop_start = monotonic_ns()
      msg = sub.recv(timeout_ms=100)
      if msg is None:
        continue
      env, payload = msg
      if env.topic == "routeProgress":
        with defs["routeProgress"].cls.from_bytes(payload) as route:
          target_from_route = float(route.targetSpeedMps)
        continue
      if env.topic == "safetyState":
        with defs["safetyState"].cls.from_bytes(payload) as safety:
          safety_mode = str(safety.mode)
        continue
      if env.topic != "vehicleState":
        continue

      with defs["vehicleState"].cls.from_bytes(payload) as state:
        target_speed = max(0.0, min(6.0, min(target_from_route, state.speedMps + 0.2)))
        if "mrs" in safety_mode:
          target_speed = 0.0

      tr = defs["plannerTrajectory"].cls.new_message()
      tr.schemaVersion = 1
      tr.confidence = 0.98 if target_speed > 0.0 else 0.7
      points = tr.init("points", 5)
      for i in range(5):
        points[i].xM = float(i * 2)
        points[i].yM = 0.0
        points[i].targetSpeedMps = target_speed
      pub.send("plannerTrajectory", "PlannerTrajectory", capnp_to_bytes(tr))

      if monotonic_ns() - hb_last > 1_000_000_000:
        hb = defs["heartbeat"].cls.new_message()
        hb.schemaVersion = 1
        hb.processName = "planner"
        hb.healthy = True
        hb.statusText = "ok"
        pub.send("heartbeat", "Heartbeat", capnp_to_bytes(hb))
        hb_last = monotonic_ns()
      sleep_to_rate(cfg.topics["plannerTrajectory"].hz, loop_start)
  finally:
    sub.close()
    pub.close()


def run_controller() -> None:
  cfg = load_config()
  defs = message_defs()
  sub = Subscriber(cfg.subscriber_address, topics=["plannerTrajectory", "safetyState"])
  pub = Publisher(cfg.publisher_address, source="controller")
  hb_last = monotonic_ns()
  emergency_stop = False
  target_speed = 0.0
  try:
    while True:
      loop_start = monotonic_ns()
      # Drain available updates without blocking the control loop rate.
      while True:
        msg = sub.recv(timeout_ms=0)
        if msg is None:
          break
        env, payload = msg
        if env.topic == "safetyState":
          with defs["safetyState"].cls.from_bytes(payload) as s:
            emergency_stop = bool(s.emergencyStop)
          continue
        if env.topic == "plannerTrajectory":
          with defs["plannerTrajectory"].cls.from_bytes(payload) as tr:
            target_speed = tr.points[0].targetSpeedMps if len(tr.points) else 0.0

      cmd = defs["controlCommand"].cls.new_message()
      cmd.schemaVersion = 1
      cmd.targetSpeedMps = 0.0 if emergency_stop else target_speed
      cmd.brakePct = 1.0 if emergency_stop else 0.0
      cmd.steeringPct = 0.0
      cmd.emergencyStop = emergency_stop
      pub.send("controlCommand", "ControlCommand", capnp_to_bytes(cmd))

      if monotonic_ns() - hb_last > 1_000_000_000:
        hb = defs["heartbeat"].cls.new_message()
        hb.schemaVersion = 1
        hb.processName = "controller"
        hb.healthy = True
        hb.statusText = "ok"
        pub.send("heartbeat", "Heartbeat", capnp_to_bytes(hb))
        hb_last = monotonic_ns()
      sleep_to_rate(cfg.topics["controlCommand"].hz, loop_start)
  finally:
    sub.close()
    pub.close()


def run_watchdog() -> None:
  cfg = load_config()
  defs = message_defs()
  sub = Subscriber(
    cfg.subscriber_address,
    topics=["heartbeat", "vehicleState", "routeProgress", "plannerTrajectory", "controlCommand", "safetyState"],
  )
  pub = Publisher(cfg.publisher_address, source="watchdog")
  start_ns = monotonic_ns()
  startup_grace_ns = int(3e9)
  last_seen: dict[str, int] = {name: start_ns for name in cfg.topics.keys()}
  last_fault_emit_ns: dict[str, int] = {}
  fault_cooldown_ns = int(1e9)
  try:
    while True:
      now = monotonic_ns()
      msg = sub.recv(timeout_ms=200)
      if msg is not None:
        env, _payload = msg
        last_seen[env.topic] = now

      faults: list[str] = []
      for name, spec in cfg.topics.items():
        if not spec.critical:
          continue
        if now - start_ns < startup_grace_ns:
          continue
        deadline_ns = int((2.5 / max(spec.hz, 0.1)) * 1e9)
        if now - last_seen.get(name, 0) > deadline_ns:
          if now - last_fault_emit_ns.get(name, 0) > fault_cooldown_ns:
            faults.append(f"stale topic: {name}")
            last_fault_emit_ns[name] = now

      for f in faults:
        evt = defs["faultEvent"].cls.new_message()
        evt.schemaVersion = 1
        evt.source = "watchdog"
        evt.severity = "critical"
        evt.message = f
        pub.send("faultEvent", "FaultEvent", capnp_to_bytes(evt))

  finally:
    sub.close()
    pub.close()


def run_route_manager(route_path: Path) -> None:
  cfg = load_config()
  defs = message_defs()
  route = load_route(route_path)
  sub = Subscriber(cfg.subscriber_address, topics=["vehicleState"])
  pub = Publisher(cfg.publisher_address, source="route_manager")
  hb_last = monotonic_ns()
  try:
    while True:
      loop_start = monotonic_ns()
      msg = sub.recv(timeout_ms=200)
      if msg is None:
        continue
      env, payload = msg
      if env.topic != "vehicleState":
        continue
      with defs["vehicleState"].cls.from_bytes(payload) as state:
        snap = snapshot_from_odom(route, float(state.wheelOdomM))
      rp = defs["routeProgress"].cls.new_message()
      rp.schemaVersion = 1
      rp.routeDistanceM = snap.route_distance_m
      rp.routeLengthM = snap.route_length_m
      rp.routePercent = snap.route_percent
      rp.targetSpeedMps = snap.target_speed_mps
      pub.send("routeProgress", "RouteProgress", capnp_to_bytes(rp))

      if monotonic_ns() - hb_last > 1_000_000_000:
        hb = defs["heartbeat"].cls.new_message()
        hb.schemaVersion = 1
        hb.processName = "route_manager"
        hb.healthy = True
        hb.statusText = "ok"
        pub.send("heartbeat", "Heartbeat", capnp_to_bytes(hb))
        hb_last = monotonic_ns()
      sleep_to_rate(cfg.topics["routeProgress"].hz, loop_start)
  finally:
    sub.close()
    pub.close()


def run_safety_manager() -> None:
  cfg = load_config()
  defs = message_defs()
  sub = Subscriber(cfg.subscriber_address, topics=["faultEvent"])
  pub = Publisher(cfg.publisher_address, source="safety_manager")
  sup = SafetySupervisor(caution_ttl_s=2.0, mrs_hold_s=5.0)
  hb_last = monotonic_ns()
  try:
    while True:
      loop_start = monotonic_ns()
      now = monotonic_ns()
      msg = sub.recv(timeout_ms=0)
      if msg is not None:
        env, payload = msg
        if env.topic == "faultEvent":
          with defs["faultEvent"].cls.from_bytes(payload) as fault:
            sup.observe_fault(str(fault.severity), str(fault.message), now)

      snap = sup.tick(now_ns=now)
      ss = defs["safetyState"].cls.new_message()
      ss.schemaVersion = 1
      ss.mode = snap.mode
      ss.reason = snap.reason
      ss.emergencyStop = snap.emergency_stop
      pub.send("safetyState", "SafetyState", capnp_to_bytes(ss))

      if monotonic_ns() - hb_last > 1_000_000_000:
        hb = defs["heartbeat"].cls.new_message()
        hb.schemaVersion = 1
        hb.processName = "safety_manager"
        hb.healthy = True
        hb.statusText = snap.mode
        pub.send("heartbeat", "Heartbeat", capnp_to_bytes(hb))
        hb_last = monotonic_ns()
      sleep_to_rate(cfg.topics["safetyState"].hz, loop_start)
  finally:
    sub.close()
    pub.close()


def run_logger(output_path: Path) -> None:
  cfg = load_config()
  topics = list(cfg.topics.keys())
  sub = Subscriber(cfg.subscriber_address, topics=topics)
  output_path.parent.mkdir(parents=True, exist_ok=True)
  with output_path.open("a", encoding="utf-8") as f:
    while True:
      msg = sub.recv(timeout_ms=1000)
      if msg is None:
        continue
      env, payload = msg
      row = {
        "topic": env.topic,
        "source": env.source,
        "sequence": env.sequence,
        "monotonic_time_ns": env.monotonic_time_ns,
        "schema": env.schema,
        "payload_b64": base64.b64encode(payload).decode("ascii"),
      }
      f.write(json.dumps(row, separators=(",", ":")) + "\n")
      f.flush()


def run_replay(input_path: Path, mode: str = "realtime") -> None:
  import time

  cfg = load_config()
  pub = Publisher(cfg.publisher_address, source="replay")
  prev_ns: int | None = None
  with input_path.open("r", encoding="utf-8") as f:
    for line in f:
      row = json.loads(line)
      now_ns = int(row["monotonic_time_ns"])
      if mode == "realtime" and prev_ns is not None:
        dt = max(0, now_ns - prev_ns)
        time.sleep(dt / 1e9)
      prev_ns = now_ns
      payload = base64.b64decode(row["payload_b64"])
      pub.send(str(row["topic"]), str(row["schema"]), payload)
