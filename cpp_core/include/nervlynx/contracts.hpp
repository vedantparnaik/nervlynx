#pragma once

#include <string>
#include <unordered_map>
#include <vector>

#include "nervlynx/runtime.hpp"

namespace nervlynx {

struct TopicContract {
  std::string topic;
  std::string schema;
  int version{1};
  std::vector<std::string> required_fields;
};

inline std::vector<std::string> validate_payload(const TopicContract& contract, const Payload& payload) {
  std::vector<std::string> issues;
  for (const auto& field : contract.required_fields) {
    if (payload.find(field) == payload.end()) {
      issues.push_back("missing required field: " + field);
    }
  }
  return issues;
}

}  // namespace nervlynx
