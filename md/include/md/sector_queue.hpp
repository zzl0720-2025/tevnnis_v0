#pragma once
// SectorQueues — the multi-level priority queues of §2.1, plus the cursor
// bookkeeping behind §4.3's `since_cursor` / `next_cursor`.
//
// Structure: one queue per sector, each holding four FIFO levels (LOW..CRITICAL)
// of sequence numbers, over a single bounded retain buffer that owns the events.
// Retention (rather than popping on pull) is what lets a pull with a stale or
// truncating cursor be served again — see the cursor contract below.
//
// CURSOR CONTRACT (v0)
//  * A cursor is "<epoch>:<seq>". `epoch` identifies this md process, `seq` is a
//    monotonic per-event counter.
//  * Durable across a CORE restart: core persists `next_cursor` and passes it
//    back (§4.3).
//  * NOT durable across an MD restart: md persists nothing (§2.2 — core is the
//    sole DB writer), so the counter resets and the epoch changes. A cursor from
//    another epoch is unrecognized, and the pull is served from the current
//    in-memory buffer; core's `event_id` unique constraint is the backstop.
//  * NEVER SKIPS: `next_cursor` is only advanced past events that were actually
//    delivered in this response, so no matching event is ever passed over.
//  * MAY RE-DELIVER: when `max_events` truncates a batch and the events left
//    behind have lower seq than some delivered ones (the batch is ordered by
//    priority, not by seq), `next_cursor` is held at (lowest undelivered seq -
//    1). The already-delivered higher-seq events are therefore re-delivered on
//    the next pull. This is deliberate — never skipping matters, re-delivering
//    does not — and core's `event_id` unique constraint is again the backstop.
//  * A pull filtered by `sectors` / `min_priority` advances the cursor past
//    events excluded by that filter, so a core that changes filters mid-stream
//    can miss the previously-excluded ones. Core uses one stable filter.

#include <array>
#include <cstdint>
#include <deque>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

#include "events.pb.h"

namespace tevnnis::md {

// Encodes/decodes "<epoch>:<seq>" cursors for one md process.
class CursorCodec {
   public:
    explicit CursorCodec(std::string epoch);

    // Generates an epoch id unique to this md process.
    static std::string NewEpochId();

    [[nodiscard]] std::string Encode(std::uint64_t seq) const;

    // std::nullopt when the cursor is empty, malformed, or from another epoch
    // (i.e. from before an md restart) — the caller then serves the whole
    // current buffer.
    [[nodiscard]] std::optional<std::uint64_t> Decode(const std::string& cursor) const;

    [[nodiscard]] const std::string& epoch() const { return epoch_; }

   private:
    std::string epoch_;
};

class SectorQueues {
   public:
    struct Selection {
        std::vector<const tevnnis::MarketEvent*> events;  // pre-sorted for the response
        std::uint64_t next_seq = 0;                       // watermark for next_cursor
        int considered = 0;                               // matching events this round
    };

    explicit SectorQueues(std::size_t capacity);

    // Appends an event, assigning it the next sequence number. Returns that
    // sequence. When the buffer is full the oldest event is evicted (and
    // counted in `evicted()`), since a stale event is not worth an LLM call.
    std::uint64_t Enqueue(tevnnis::MarketEvent event);

    // Selects up to `max_events` events with seq > `since_seq`, keeping only
    // the requested sectors (empty = all; market-wide events with an empty
    // sector always pass) and priorities >= `min_priority`. Ordered by
    // priority desc, then event_ts asc, then seq asc.
    [[nodiscard]] Selection Select(std::uint64_t since_seq, int max_events,
                                   tevnnis::Priority min_priority,
                                   const std::vector<std::string>& sectors) const;

    [[nodiscard]] std::uint64_t last_seq() const { return next_seq_ - 1; }
    [[nodiscard]] std::size_t size() const { return buffer_.size(); }
    [[nodiscard]] std::int64_t evicted() const { return evicted_; }

    // Number of retained events currently queued at `priority` in `sector`.
    [[nodiscard]] std::size_t LevelSize(const std::string& sector,
                                        tevnnis::Priority priority) const;

   private:
    struct Buffered {
        std::uint64_t seq = 0;
        tevnnis::MarketEvent event;
    };

    static constexpr std::size_t kLevels = 4;  // LOW, MEDIUM, HIGH, CRITICAL
    using SectorLevels = std::array<std::deque<std::uint64_t>, kLevels>;

    void DropOldest();
    [[nodiscard]] const Buffered* Find(std::uint64_t seq) const;

    std::size_t capacity_;
    std::deque<Buffered> buffer_;  // ascending seq, owns the events
    std::unordered_map<std::string, SectorLevels> sectors_;
    std::uint64_t next_seq_ = 1;  // seq 0 is reserved: "nothing delivered yet"
    std::int64_t evicted_ = 0;
};

}  // namespace tevnnis::md
