#pragma once

#include <string>
#include <vector>

namespace nervlynx {
struct GeneratedContract {
  std::string topic;
  std::string schema;
  int version;
  std::vector<std::string> required_fields;
};

inline std::vector<GeneratedContract> generated_contracts() {
  return {
    GeneratedContract{"sensors.bundle", "SensorBundle", 1, {"camera_count", "gps_fix", "imu_ok", "ai_ok"}},
    GeneratedContract{"perception.scene", "SceneState", 1, {"confidence", "motion_detected", "audio_event", "gps_fix"}},
    GeneratedContract{"mission.command", "MissionCommand", 1, {"mode", "speed_limit_mps", "waypoint_id"}},
  };
}
}
