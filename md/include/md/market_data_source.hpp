#pragma once
// Minimal source interface for the md data plane (§2.2, §3, §13).
//
// Both the Longbridge QuoteContext adapter (later stage) and
// MockMarketDataSource implements this interface. It is deliberately
// thin: replay/deliver timed quote, news and status events in order. The Data
// Unifier (normalize + dedup) and sector priority queues that consume a source
// live downstream in md/pipeline.hpp.

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace tevnnis::md {

enum class SourceEventType { kQuote, kNews, kStatus };

// Raw quote tick as seen at the source, before Data Unifier normalization.
struct QuoteEvent {
    std::string symbol;
    double last_price = 0.0;
    double prev_close = 0.0;
    int64_t volume = 0;
    int64_t event_ts = 0;  // epoch millis, when it happened at source
};

// Raw news item as seen at the source (§4.2: title only, no article body).
struct NewsEvent {
    std::string news_id;
    std::string title;
    std::string source;
    std::string url;
    std::vector<std::string> related_symbols;
    int64_t event_ts = 0;  // epoch millis, when it happened at source
};

// Mirrors StatusPayload.Status in events.proto (§4.2).
enum class SourceStatus { kHalted, kResumed, kPreMarket, kPostMarket, kClosed };

// Trading-status change at the source. Halt/resume on a universe symbol is one
// of the three narrow CRITICAL cases in §4.4.
struct StatusEvent {
    std::string symbol;  // empty for a market-wide status (PRE_MARKET/CLOSED/...)
    SourceStatus status = SourceStatus::kHalted;
    int64_t event_ts = 0;  // epoch millis, when it happened at source
};

struct SourceEvent {
    SourceEventType type = SourceEventType::kQuote;
    QuoteEvent quote;    // valid when type == kQuote
    NewsEvent news;      // valid when type == kNews
    StatusEvent status;  // valid when type == kStatus
};

// Source timestamp of an event, whichever variant it holds.
inline int64_t SourceEventTs(const SourceEvent& ev) {
    switch (ev.type) {
        case SourceEventType::kQuote:
            return ev.quote.event_ts;
        case SourceEventType::kNews:
            return ev.news.event_ts;
        case SourceEventType::kStatus:
            return ev.status.event_ts;
    }
    return 0;
}

// Source interface: deliver events, in source-timestamp order, to a sink
// callback. A real adapter streams live from the broker's QuoteContext /
// news feed; a mock replays a fixed, scripted sequence.
class MarketDataSource {
   public:
    virtual ~MarketDataSource() = default;

    // Deliver every available event to `on_event`, in ascending event_ts
    // order. Implementations decide whether this blocks (a live stream) or
    // returns once the scripted sequence is exhausted (a replay source).
    virtual void run(const std::function<void(const SourceEvent&)>& on_event) = 0;
};

}  // namespace tevnnis::md
