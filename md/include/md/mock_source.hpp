#pragma once
// MockMarketDataSource — replays a scripted JSON scenario file (§13 mock-first).
//
// Loads a scenario file (see md/scenarios/example_basic.json for the format)
// at construction, parses it into a time-ordered sequence of SourceEvents,
// and replays them synchronously via run(). No real-time delay, no
// subscription semantics — deterministic and offline, so the downstream
// pipeline can be driven reproducibly in tests without a live connection.

#include <stdexcept>
#include <string>
#include <vector>

#include "md/market_data_source.hpp"

namespace tevnnis::md {

// Thrown when the scenario file is missing or malformed.
class MockSourceError : public std::runtime_error {
   public:
    explicit MockSourceError(const std::string& what) : std::runtime_error(what) {}
};

class MockMarketDataSource : public MarketDataSource {
   public:
    // Parses `scenario_path` immediately; throws MockSourceError on failure.
    explicit MockMarketDataSource(std::string scenario_path);

    // Replays every parsed event, in ascending event_ts order, via on_event.
    void run(const std::function<void(const SourceEvent&)>& on_event) override;

    // Test helper: number of events parsed from the scenario file.
    [[nodiscard]] size_t event_count() const { return events_.size(); }

   private:
    std::string scenario_path_;
    std::vector<SourceEvent> events_;
};

}  // namespace tevnnis::md
