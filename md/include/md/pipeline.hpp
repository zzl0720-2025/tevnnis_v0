#pragma once
// MdPipeline — the md data plane wired end to end (§2.2).
//
//   source event -> DataUnifier -> QuoteThrottle -> AssignPriority
//                -> SectorQueues -> PullDecisionBatch
//
// Everything the gRPC service does lives here, so the whole plane is testable
// without a server. The pipeline holds no durable state: it never writes to the
// database (core is the sole writer) and never persists cursors or delivery
// state — see the cursor contract in sector_queue.hpp.

#include <cstdint>
#include <functional>
#include <mutex>
#include <string>

#include "events.pb.h"
#include "md/data_unifier.hpp"
#include "md/market_data_source.hpp"
#include "md/pipeline_config.hpp"
#include "md/sector_queue.hpp"
#include "md/throttle.hpp"
#include "md_service.pb.h"

namespace tevnnis::md {

// Epoch-millis clock. Replay and tests inject a deterministic one so cooldown
// and rate-cap windows advance with scenario time rather than wall time.
using Clock = std::function<std::int64_t()>;

// Wall-clock source, the default outside tests.
std::int64_t SystemClockMs();

// A settable clock for replay: the driver advances it to each source event's
// timestamp before handing the event to the pipeline.
class ReplayClock {
   public:
    void set_now_ms(std::int64_t now_ms) { now_ms_ = now_ms; }
    [[nodiscard]] std::int64_t now_ms() const { return now_ms_; }
    [[nodiscard]] Clock AsClock() { return [this]() { return now_ms_; }; }

   private:
    std::int64_t now_ms_ = 0;
};

class MdPipeline {
   public:
    struct Stats {
        std::int64_t ingested = 0;          // source events seen
        std::int64_t outside_universe = 0;  // filtered by the §6 allowlist
        std::int64_t emitted = 0;           // events enqueued for core
        std::int64_t dedup_exact = 0;
        std::int64_t dedup_near = 0;
        std::int64_t throttled_same_band = 0;
        std::int64_t throttled_cooldown = 0;
        std::int64_t throttled_rate_cap = 0;
        std::int64_t evicted = 0;  // aged out of the retain buffer
    };

    // `config` is copied and Finalize()d; throws ConfigError if invalid.
    // `epoch_id` identifies this md process in cursors — pass an explicit one
    // only in tests that need to forge a cursor from another process.
    explicit MdPipeline(PipelineConfig config, Clock clock = SystemClockMs,
                        std::string epoch_id = CursorCodec::NewEpochId());

    // Normalizes, throttles, prioritizes and enqueues one source event.
    // Returns true when the event was enqueued for core.
    bool OnSourceEvent(const SourceEvent& source_event);

    // §4.3 PullDecisionBatch. Honors max_events, min_priority, sectors and
    // since_cursor; returns prioritized events, compact sector snapshots, the
    // next cursor and the number of events dropped/collapsed since the last
    // pull (which this call resets).
    [[nodiscard]] tevnnis::PullResponse Pull(const tevnnis::PullRequest& request);

    [[nodiscard]] Stats stats() const;
    [[nodiscard]] const std::string& epoch_id() const { return codec_.epoch(); }

   private:
    mutable std::mutex mutex_;
    PipelineConfig config_;
    Clock clock_;
    CursorCodec codec_;
    DataUnifier unifier_;
    QuoteThrottle throttle_;
    SectorQueues queues_;
    Stats stats_;
    std::int32_t dropped_since_last_pull_ = 0;
};

}  // namespace tevnnis::md
