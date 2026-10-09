#pragma once
// Normalize source events, filter the universe and deduplicate news.
// Compute quote statistics and deterministic event IDs. Snapshot state is
// updated on every in-universe tick, including ticks suppressed by throttling.
// The pipeline sets QuotePayload.trigger after QuoteThrottle accepts a tick.

#include <cstdint>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

#include "events.pb.h"
#include "md/dedup.hpp"
#include "md/market_data_source.hpp"
#include "md/pipeline_config.hpp"
#include "md_service.pb.h"

namespace tevnnis::md {

class DataUnifier {
   public:
    enum class Outcome {
        kEvent,           // `event` holds a normalized MarketEvent
        kOutsideUniverse, // symbol(s) not in the allowlist: dropped, not counted
        kDuplicateExact,  // same news_id already seen in the window
        kDuplicateNear,   // different id, substantially the same headline
        kInvalid,         // unusable input (e.g. prev_close <= 0)
    };

    struct Result {
        Outcome outcome = Outcome::kOutsideUniverse;
        std::optional<tevnnis::MarketEvent> event;
    };

    // `config` must outlive the unifier and must already be Finalize()d.
    explicit DataUnifier(const PipelineConfig& config);

    // Normalizes one source event. `ingest_ts_ms` becomes MarketEvent.ingest_ts
    // and drives the de-duplication window.
    Result Unify(const SourceEvent& source_event, std::int64_t ingest_ts_ms);

    // Compact per-sector state for the response. An empty `sectors` means
    // every sector. Symbols appear in universe order, and only once a tick for
    // them has been seen.
    [[nodiscard]] std::vector<tevnnis::SectorSnapshot> SnapshotsFor(
        const std::vector<std::string>& sectors) const;

   private:
    // Latest known state per symbol; the snapshot source of truth.
    struct SymbolState {
        double last_price = 0.0;
        double prev_close = 0.0;
        double change_pct = 0.0;
        double intraday_high = 0.0;
        double intraday_low = 0.0;
        std::int64_t volume = 0;
        std::int64_t last_ts = 0;
    };

    Result UnifyQuote(const QuoteEvent& quote, std::int64_t ingest_ts_ms);
    Result UnifyNews(const NewsEvent& news, std::int64_t ingest_ts_ms);
    Result UnifyStatus(const StatusEvent& status, std::int64_t ingest_ts_ms);

    const PipelineConfig& config_;
    DedupWindow dedup_;
    std::unordered_map<std::string, SymbolState> states_;
};

}  // namespace tevnnis::md
