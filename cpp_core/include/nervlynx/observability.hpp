#pragma once

#include <string>
#include <unordered_map>
#include <vector>

#include "nervlynx/runtime.hpp"

namespace nervlynx {

struct TopicStats {
  std::string topic;
  std::size_t count{0};
};

inline std::vector<TopicStats> topic_stats(const std::vector<RuntimeMessage>& trace) {
  std::unordered_map<std::string, std::size_t> counts;
  for (const auto& msg : trace) {
    counts[msg.envelope.topic] += 1;
  }
  std::vector<TopicStats> out;
  out.reserve(counts.size());
  for (const auto& [topic, count] : counts) {
    out.push_back(TopicStats{topic, count});
  }
  return out;
}

}  // namespace nervlynx
