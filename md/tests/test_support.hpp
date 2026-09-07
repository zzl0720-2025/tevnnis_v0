#pragma once
// Shared fixtures for the md pipeline tests: a config mirroring the §6 example
// and terse builders for raw source events.

#include <string>
#include <vector>

#include "md/market_data_source.hpp"
#include "md/pipeline_config.hpp"

namespace tevnnis::md::test {

// Universe and knobs from config/config.example.yaml. Universe order matters:
// Semiconductor comes first, so it wins for news naming symbols in two sectors.
inline PipelineConfig BaseConfig() {
    PipelineConfig config;
    config.universe = {
        {"Semiconductor", {"NVDA.US", "AMD.US"}},
        {"Energy", {"XOM.US"}},
        {"Web", {"GOOGL.US", "META.US"}},
        {"BroadETF", {"SPY.US", "QQQ.US", "VOO.US"}},
    };
    config.entry_threshold_pct = 3.0;
    config.escalation_bands = {3.0, 5.0, 8.0};
    config.cooldown_minutes = 15;
    config.sector_rate_cap_per_hour = 12;
    config.critical_move_pct = 8.0;
    config.Finalize();
    return config;
}

inline SourceEvent Quote(const std::string& symbol, double last_price, double prev_close,
                         std::int64_t event_ts, std::int64_t volume = 0) {
    SourceEvent event;
    event.type = SourceEventType::kQuote;
    event.quote.symbol = symbol;
    event.quote.last_price = last_price;
    event.quote.prev_close = prev_close;
    event.quote.volume = volume;
    event.quote.event_ts = event_ts;
    return event;
}

inline SourceEvent News(const std::string& news_id, const std::string& title,
                        std::int64_t event_ts, std::vector<std::string> related_symbols,
                        const std::string& source = "Reuters",
                        const std::string& url = "https://example.com/x") {
    SourceEvent event;
    event.type = SourceEventType::kNews;
    event.news.news_id = news_id;
    event.news.title = title;
    event.news.source = source;
    event.news.url = url;
    event.news.related_symbols = std::move(related_symbols);
    event.news.event_ts = event_ts;
    return event;
}

inline SourceEvent Status(const std::string& symbol, SourceStatus status,
                          std::int64_t event_ts) {
    SourceEvent event;
    event.type = SourceEventType::kStatus;
    event.status.symbol = symbol;
    event.status.status = status;
    event.status.event_ts = event_ts;
    return event;
}

inline std::string ScenarioFile(const std::string& name) {
    return std::string(TEVNNIS_MD_SCENARIOS_DIR) + "/" + name;
}

constexpr std::int64_t kMinuteMs = 60 * 1000;
constexpr std::int64_t kBaseTs = 1700000000000;  // matches stage3_pipeline.json

}  // namespace tevnnis::md::test
