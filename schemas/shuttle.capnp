@0xca8fbf8a86f9568f;

struct Header {
  schemaVersion @0 :UInt16;
  source @1 :Text;
  topic @2 :Text;
  sequence @3 :UInt64;
  monotonicTimeNs @4 :UInt64;
}

struct VehicleState {
  schemaVersion @0 :UInt16;
  speedMps @1 :Float32;
  yawRateRps @2 :Float32;
  wheelOdomM @3 :Float64;
  latDeg @4 :Float64;
  lonDeg @5 :Float64;
  headingDeg @6 :Float32;
}

struct TrajectoryPoint {
  xM @0 :Float32;
  yM @1 :Float32;
  targetSpeedMps @2 :Float32;
}

struct PlannerTrajectory {
  schemaVersion @0 :UInt16;
  confidence @1 :Float32;
  points @2 :List(TrajectoryPoint);
}

struct ControlCommand {
  schemaVersion @0 :UInt16;
  targetSpeedMps @1 :Float32;
  brakePct @2 :Float32;
  steeringPct @3 :Float32;
  emergencyStop @4 :Bool;
}

struct Heartbeat {
  schemaVersion @0 :UInt16;
  processName @1 :Text;
  healthy @2 :Bool;
  statusText @3 :Text;
}

struct FaultEvent {
  schemaVersion @0 :UInt16;
  source @1 :Text;
  severity @2 :Text;
  message @3 :Text;
}

struct RouteProgress {
  schemaVersion @0 :UInt16;
  routeDistanceM @1 :Float32;
  routeLengthM @2 :Float32;
  routePercent @3 :Float32;
  targetSpeedMps @4 :Float32;
}

enum SafetyMode {
  normal @0;
  caution @1;
  mrs @2;
}

struct SafetyState {
  schemaVersion @0 :UInt16;
  mode @1 :SafetyMode;
  reason @2 :Text;
  emergencyStop @3 :Bool;
}
