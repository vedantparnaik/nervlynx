#pragma once

#include <chrono>
#include <cstdint>
#include <deque>
#include <functional>
#include <string>
#include <unordered_map>
#include <utility>
#include <variant>
#include <vector>

namespace nervlynx {

using Value = std::variant<double, bool, std::string>;
using Payload = std::unordered_map<std::string, Value>;

struct Envelope {
  std::string topic;
  std::string source;
  std::uint64_t sequence;
  std::uint64_t monotonic_time_ns;
  std::string trace_id;
  std::string schema;
};

struct RuntimeMessage {
  Envelope envelope;
  Payload payload;
};

struct PublishRequest {
  std::string topic;
  std::string schema;
  Payload payload;
};

using NodeHandler = std::function<std::vector<PublishRequest>(const RuntimeMessage&)>;

class PipelineRuntime {
public:
  void subscribe(const std::string& node_name, const std::string& topic, NodeHandler handler) {
    subscriptions_[topic].push_back({node_name, std::move(handler)});
  }

  RuntimeMessage publish(
    const std::string& topic,
    const std::string& source,
    const std::string& schema,
    const Payload& payload,
    const std::string& trace_id = ""
  ) {
    const auto key = source + ":" + topic;
    const auto seq = ++sequences_[key];
    RuntimeMessage msg{
      Envelope{
        topic,
        source,
        seq,
        now_ns(),
        trace_id.empty() ? make_trace_id(seq) : trace_id,
        schema,
      },
      payload,
    };
    return msg;
  }

  std::vector<RuntimeMessage> run_once(const RuntimeMessage& root, std::size_t max_hops = 256) {
    std::vector<RuntimeMessage> seen;
    std::deque<RuntimeMessage> queue;
    queue.push_back(root);
    std::size_t hops = 0;

    while (!queue.empty()) {
      ++hops;
      if (hops > max_hops) {
        faults_.push_back("pipeline hop limit reached");
        break;
      }

      auto current = queue.front();
      queue.pop_front();
      seen.push_back(current);

      const auto it = subscriptions_.find(current.envelope.topic);
      if (it == subscriptions_.end()) {
        continue;
      }

      for (const auto& sub : it->second) {
        const auto& node_name = sub.first;
        const auto& handler = sub.second;
        const auto out = handler(current);
        heartbeats_ns_[node_name] = now_ns();
        for (const auto& req : out) {
          queue.push_back(
            publish(req.topic, node_name, req.schema, req.payload, current.envelope.trace_id)
          );
        }
      }
    }

    return seen;
  }

  const std::unordered_map<std::string, std::uint64_t>& node_heartbeats_ns() const {
    return heartbeats_ns_;
  }

  const std::vector<std::string>& faults() const {
    return faults_;
  }

private:
  std::unordered_map<std::string, std::vector<std::pair<std::string, NodeHandler>>> subscriptions_;
  std::unordered_map<std::string, std::uint64_t> sequences_;
  std::unordered_map<std::string, std::uint64_t> heartbeats_ns_;
  std::vector<std::string> faults_;

  static std::uint64_t now_ns() {
    const auto now = std::chrono::steady_clock::now().time_since_epoch();
    return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(now).count()
    );
  }

  static std::string make_trace_id(std::uint64_t seq) {
    return "trace-" + std::to_string(now_ns()) + "-" + std::to_string(seq);
  }
};

inline bool payload_bool(const Payload& p, const std::string& key, bool fallback = false) {
  const auto it = p.find(key);
  if (it == p.end()) {
    return fallback;
  }
  if (const auto* v = std::get_if<bool>(&it->second)) {
    return *v;
  }
  return fallback;
}

inline double payload_double(const Payload& p, const std::string& key, double fallback = 0.0) {
  const auto it = p.find(key);
  if (it == p.end()) {
    return fallback;
  }
  if (const auto* v = std::get_if<double>(&it->second)) {
    return *v;
  }
  return fallback;
}

}  // namespace nervlynx
