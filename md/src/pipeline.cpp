#include "md/pipeline.hpp"

#include <chrono>
#include <utility>

#include "md/priority.hpp"

namespace tevnnis::md {

namespace {

// Validates and indexes the config before any member can reference it.
PipelineConfig Finalized(PipelineConfig config) {
    config.Finalize();
    return config;
}

}  // namespace

std::int64_t SystemClockMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

MdPipeline::MdPipeline(PipelineConfig config, Clock clock, std::string epoch_id)
    : config_(Finalized(std::move(config))),
      clock_(std::move(clock)),
      codec_(std::move(epoch_id)),
      unifier_(config_),
      throttle_(config_),
      queues_(config_.retain_buffer_capacity) {}

bool MdPipeline::OnSourceEvent(const SourceEvent& source_event) {
    const std::lock_guard<std::mutex> lock(mutex_);
    const std::int64_t now_ms = clock_();
    ++stats_.ingested;

    DataUnifier::Result unified = unifier_.Unify(source_event, now_ms);
    switch (unified.outcome) {
        case DataUnifier::Outcome::kOutsideUniverse:
            ++stats_.outside_universe;  // not a drop: it was never ours to send
            return false;
        case DataUnifier::Outcome::kInvalid:
            return false;
        case DataUnifier::Outcome::kDuplicateExact:
            ++stats_.dedup_exact;
            ++dropped_since_last_pull_;
            return false;
        case DataUnifier::Outcome::kDuplicateNear:
            ++stats_.dedup_near;
            ++dropped_since_last_pull_;
            return false;
        case DataUnifier::Outcome::kEvent:
            break;
    }

    tevnnis::MarketEvent& event = *unified.event;

    if (event.type() == tevnnis::QUOTE_MOVE) {
        const ThrottleResult decision = throttle_.Evaluate(
            event.symbol(), event.sector(), event.quote().change_pct(), now_ms);
        switch (decision.decision) {
            case ThrottleDecision::kBelowEntryThreshold:
                // Never eligible — a quiet tick is not a dropped event, it only
                // refreshes the snapshot state.
                return false;
            case ThrottleDecision::kSameOrLowerBand:
                ++stats_.throttled_same_band;
                ++dropped_since_last_pull_;
                return false;
            case ThrottleDecision::kCooldown:
                ++stats_.throttled_cooldown;
                ++dropped_since_last_pull_;
                return false;
            case ThrottleDecision::kSectorRateCap:
                ++stats_.throttled_rate_cap;
                ++dropped_since_last_pull_;
                return false;
            case ThrottleDecision::kEmit:
                event.mutable_quote()->set_trigger(decision.trigger);
                break;
        }
    }

    event.set_priority(AssignPriority(event, config_));

    const std::int64_t evicted_before = queues_.evicted();
    queues_.Enqueue(std::move(event));
    const std::int64_t newly_evicted = queues_.evicted() - evicted_before;
    if (newly_evicted > 0) {
        stats_.evicted += newly_evicted;
        dropped_since_last_pull_ += static_cast<std::int32_t>(newly_evicted);
    }
    ++stats_.emitted;
    return true;
}

tevnnis::PullResponse MdPipeline::Pull(const tevnnis::PullRequest& request) {
    const std::lock_guard<std::mutex> lock(mutex_);

    const std::vector<std::string> sectors(request.sectors().begin(), request.sectors().end());

    // An unrecognized cursor (empty, malformed, or from a previous md process)
    // means "serve the current buffer" — see the cursor contract in
    // sector_queue.hpp. Core's event_id unique constraint is the backstop.
    const std::optional<std::uint64_t> since_seq = codec_.Decode(request.since_cursor());

    const SectorQueues::Selection selection = queues_.Select(
        since_seq.value_or(0), request.max_events(), request.min_priority(), sectors);

    tevnnis::PullResponse response;
    for (const tevnnis::MarketEvent* event : selection.events) {
        *response.add_events() = *event;
    }
    for (auto& snapshot : unifier_.SnapshotsFor(sectors)) {
        *response.add_snapshots() = std::move(snapshot);
    }
    response.set_next_cursor(codec_.Encode(selection.next_seq));
    response.set_dropped_count(dropped_since_last_pull_);
    dropped_since_last_pull_ = 0;
    return response;
}

MdPipeline::Stats MdPipeline::stats() const {
    const std::lock_guard<std::mutex> lock(mutex_);
    return stats_;
}

}  // namespace tevnnis::md
