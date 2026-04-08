#include <algorithm>
#include <filesystem>
#include <iostream>
#include <string>
#include <unordered_set>
#include <vector>

#include "nervlynx/contracts.hpp"
#include "nervlynx/observability.hpp"
#include "nervlynx/recorder.hpp"
#include "nervlynx/runtime.hpp"
#include "nervlynx/watchdog.hpp"

namespace {

using nervlynx::Payload;
using nervlynx::PipelineRuntime;
using nervlynx::PublishRequest;
using nervlynx::RuntimeMessage;

PipelineRuntime build_surveillance_runtime() {
  PipelineRuntime rt;

  rt.subscribe("perception_ai", "sensors.bundle", [](const RuntimeMessage& msg) {
    const bool cameras_ok = nervlynx::payload_double(msg.payload, "camera_count", 0.0) >= 3.0;
    const bool imu_ok = nervlynx::payload_bool(msg.payload, "imu_ok", false);
    const bool gps_fix = nervlynx::payload_bool(msg.payload, "gps_fix", false);
    const bool ai_ok = nervlynx::payload_bool(msg.payload, "ai_ok", false);
    double confidence = 0.3;
    if (cameras_ok) {
      confidence += 0.25;
    }
    if (imu_ok) {
      confidence += 0.2;
    }
    if (gps_fix) {
      confidence += 0.15;
    }
    if (ai_ok) {
      confidence += 0.1;
    }
    if (confidence > 1.0) {
      confidence = 1.0;
    }
    return std::vector<PublishRequest>{
      PublishRequest{
        "perception.scene",
        "SceneState",
        Payload{
          {"confidence", confidence},
          {"motion_detected", nervlynx::payload_bool(msg.payload, "motion_detected", false)},
          {"audio_event", nervlynx::payload_bool(msg.payload, "mic_anomaly", false)},
          {"gps_fix", gps_fix},
        },
      },
    };
  });

  rt.subscribe("mission_manager", "perception.scene", [](const RuntimeMessage& msg) {
    const double confidence = nervlynx::payload_double(msg.payload, "confidence", 0.0);
    const bool gps_fix = nervlynx::payload_bool(msg.payload, "gps_fix", false);
    const bool motion_detected = nervlynx::payload_bool(msg.payload, "motion_detected", false);
    const bool audio_event = nervlynx::payload_bool(msg.payload, "audio_event", false);
    const bool autonomous = confidence >= 0.7 && gps_fix;
    const std::string mode = autonomous ? "autonomous_patrol" : "safe_slow_patrol";
    const double speed_limit_mps = autonomous ? 1.4 : 0.6;

    std::vector<PublishRequest> out{
      PublishRequest{
        "mission.command",
        "MissionCommand",
        Payload{
          {"mode", mode},
          {"speed_limit_mps", speed_limit_mps},
          {"waypoint_id", std::string("colony-sector-c")},
        },
      },
    };
    if (motion_detected || audio_event) {
      out.push_back(
        PublishRequest{
          "hq.alert",
          "SecurityAlert",
          Payload{
            {"severity", std::string("medium")},
            {"event", std::string("intrusion_candidate")},
            {"needs_operator_ack", true},
          },
        }
      );
    }
    return out;
  });

  rt.subscribe("drive_controller", "mission.command", [](const RuntimeMessage& msg) {
    const double speed = nervlynx::payload_double(msg.payload, "speed_limit_mps", 0.0);
    const double motor = std::min(0.5, speed / 3.0);
    return std::vector<PublishRequest>{
      PublishRequest{
        "actuation.command",
        "WheelControl",
        Payload{
          {"left_motor_pct", motor},
          {"right_motor_pct", motor},
          {"brake_pct", 0.0},
        },
      },
    };
  });

  rt.subscribe("hq_uplink", "actuation.command", [](const RuntimeMessage&) {
    return std::vector<PublishRequest>{
      PublishRequest{
        "hq.telemetry",
        "TelemetryUplink",
        Payload{
          {"link", std::string("wifi")},
          {"robot_id", std::string("sr-3w-demo")},
          {"status", std::string("online")},
          {"battery_pct", 74.0},
        },
      },
    };
  });

  return rt;
}

}  // namespace

int main() {
  std::filesystem::create_directories("logs");
  auto rt = build_surveillance_runtime();
  const auto seed = rt.publish(
    "sensors.bundle",
    "sensor_hub",
    "SensorBundle",
    Payload{
      {"camera_count", 4.0},
      {"gps_fix", true},
      {"imu_ok", true},
      {"ai_ok", true},
      {"wifi_rssi_dbm", -61.0},
      {"motion_detected", true},
      {"mic_anomaly", false},
      {"speaker_ok", true},
    }
  );
  const nervlynx::TopicContract seed_contract{
    "sensors.bundle",
    "SensorBundle",
    1,
    {"camera_count", "gps_fix", "imu_ok", "ai_ok"},
  };
  const auto contract_issues = nervlynx::validate_payload(seed_contract, seed.payload);
  if (!contract_issues.empty()) {
    std::cerr << "FAIL contract validation\n";
    return 1;
  }

  const auto trace = rt.run_once(seed);
  nervlynx::write_jsonl("logs/smoke_surveillance_trace_cpp.jsonl", trace);

  std::unordered_set<std::string> topics;
  std::vector<std::string> ordered_topics;
  for (const auto& msg : trace) {
    topics.insert(msg.envelope.topic);
    ordered_topics.push_back(msg.envelope.topic);
  }

  std::uint64_t max_hb = 0;
  for (const auto& [_, ts] : rt.node_heartbeats_ns()) {
    if (ts > max_hb) {
      max_hb = ts;
    }
  }
  const nervlynx::HealthWatchdog watchdog(0.75);
  const auto faults = watchdog.check(rt.node_heartbeats_ns(), max_hb);
  const auto stats = nervlynx::topic_stats(trace);

  const std::unordered_set<std::string> required{
    "sensors.bundle",
    "perception.scene",
    "mission.command",
    "actuation.command",
    "hq.telemetry",
    "hq.alert",
  };

  bool ok = true;
  for (const auto& req : required) {
    if (topics.count(req) == 0) {
      ok = false;
      break;
    }
  }
  ok = ok && faults.empty() && trace.size() >= 6;

  std::cout << (ok ? "PASS" : "FAIL") << " messages=" << trace.size() << " topics=";
  for (std::size_t i = 0; i < ordered_topics.size(); ++i) {
    std::cout << ordered_topics[i];
    if (i + 1 < ordered_topics.size()) {
      std::cout << ",";
    }
  }
  std::cout << " output=logs/smoke_surveillance_trace_cpp.jsonl";
  if (!faults.empty()) {
    std::cout << " watchdog_faults=" << faults.size();
  }
  std::cout << " topic_stats=" << stats.size();
  std::cout << "\n";

  return ok ? 0 : 1;
}
