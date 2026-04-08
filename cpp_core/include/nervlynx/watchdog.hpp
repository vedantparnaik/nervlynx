#pragma once

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

namespace nervlynx {

struct WatchdogFault {
  std::string node_name;
  double age_s;
  std::string message;
};

class HealthWatchdog {
public:
  explicit HealthWatchdog(double stale_after_s = 1.5)
      : stale_after_ns_(static_cast<std::uint64_t>((stale_after_s > 0.01 ? stale_after_s : 0.01) * 1e9)) {}

  std::vector<WatchdogFault> check(
    const std::unordered_map<std::string, std::uint64_t>& heartbeats_ns,
    std::uint64_t now_ns
  ) const {
    std::vector<WatchdogFault> faults;
    for (const auto& [node_name, last_ns] : heartbeats_ns) {
      const auto age_ns = now_ns > last_ns ? now_ns - last_ns : 0;
      if (age_ns > stale_after_ns_) {
        const double age_s = static_cast<double>(age_ns) / 1e9;
        faults.push_back(
          WatchdogFault{node_name, age_s, "node " + node_name + " stale for " + std::to_string(age_s) + "s"}
        );
      }
    }
    return faults;
  }

private:
  std::uint64_t stale_after_ns_;
};

}  // namespace nervlynx
