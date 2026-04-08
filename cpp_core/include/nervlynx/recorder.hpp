#pragma once

#include <fstream>
#include <string>
#include <variant>
#include <vector>

#include "nervlynx/runtime.hpp"

namespace nervlynx {

inline std::string value_to_json(const Value& v) {
  if (const auto* d = std::get_if<double>(&v)) {
    return std::to_string(*d);
  }
  if (const auto* b = std::get_if<bool>(&v)) {
    return *b ? "true" : "false";
  }
  const auto* s = std::get_if<std::string>(&v);
  return "\"" + (*s) + "\"";
}

inline void write_jsonl(const std::string& path, const std::vector<RuntimeMessage>& trace) {
  std::ofstream out(path);
  for (const auto& msg : trace) {
    out << "{";
    out << "\"topic\":\"" << msg.envelope.topic << "\",";
    out << "\"source\":\"" << msg.envelope.source << "\",";
    out << "\"sequence\":" << msg.envelope.sequence << ",";
    out << "\"monotonic_time_ns\":" << msg.envelope.monotonic_time_ns << ",";
    out << "\"trace_id\":\"" << msg.envelope.trace_id << "\",";
    out << "\"schema\":\"" << msg.envelope.schema << "\",";
    out << "\"payload\":{";
    bool first = true;
    for (const auto& [k, v] : msg.payload) {
      if (!first) {
        out << ",";
      }
      first = false;
      out << "\"" << k << "\":" << value_to_json(v);
    }
    out << "}}";
    out << "\n";
  }
}

}  // namespace nervlynx
