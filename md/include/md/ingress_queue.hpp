#pragma once
// IngressQueue — the thread-safe hand-off between the Longbridge SDK's callback
// threads and md's single ingest thread.
//
// WHY IT EXISTS. The pipeline also supports synchronous replay:
// MockMarketDataSource::run() drains a scenario and returns BEFORE the gRPC
// server starts serving. A live QuoteContext instead delivers pushes over time,
// from a multi-threaded tokio runtime, while the server must already be
// serving. That needs an explicit concurrency model:
//
//   SDK tokio thread(s)        md ingest thread            gRPC handler threads
//   ───────────────────        ────────────────            ────────────────────
//   on_quote callback          loop:                        PullDecisionBatch
//     unwrap PushQuote           tick = ingress.Pop()         pipeline.Pull(req)
//     -> LongbridgeTick          mapper.Map(tick)                    │
//     ingress.Push(tick) ──────► pipeline.OnSourceEvent(ev)          │
//          (never touches             │                             │
//           the pipeline)        [MdPipeline::mutex_] ◄── same lock ─┘
//
// Correctness of the pipeline itself is NOT provided by this queue: MdPipeline
// is already fully mutex-guarded (OnSourceEvent, Pull and stats each take
// mutex_, and unifier_/throttle_/queues_/codec_ are touched only inside them),
// so a producer thread is safe against the gRPC handlers today. What the queue
// adds is:
//   * mapper state that is single-threaded BY CONSTRUCTION — Map() runs only on
//     the ingest thread, so the prev_close and halt-edge tables need no lock and
//     rely on no assumption about whether the SDK serializes its callbacks;
//   * latency isolation — the SDK callback never blocks behind an in-flight
//     Pull, so a slow consumer cannot stall the SDK's event loop;
//   * bounded memory with an explicit, type-aware backpressure policy.
//
// Only one consumer thread is supported, by design: a single ingest thread is
// what gives OnSourceEvent a well-defined order, so throttle bands, cooldowns
// and dedup evolve exactly as they do in the deterministic replay path.

#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <utility>

#include "md/longbridge_mapping.hpp"

namespace tevnnis::md {

class IngressQueue {
   public:
    static constexpr std::size_t kDefaultCapacity = 4096;

    // Defensive ceiling for unevictable ticks. Reaching it would mean thousands
    // of queued halt-class ticks with a wedged consumer; it should be
    // unreachable (a halted symbol stops ticking, and the universe is small),
    // but an unbounded queue is not an acceptable alternative.
    static constexpr std::size_t kProtectedOverflowFactor = 4;

    struct Stats {
        std::int64_t pushed = 0;
        std::int64_t popped = 0;
        std::int64_t dropped_quotes = 0;     // evicted/refused under backpressure
        std::int64_t dropped_protected = 0;  // must stay 0 — see above
        std::size_t high_water = 0;
    };

    explicit IngressQueue(std::size_t capacity = kDefaultCapacity) : capacity_(capacity) {}

    // Producer side, called from SDK callback threads. Never blocks on the
    // pipeline and never waits for the consumer.
    //
    // Overflow is TYPE-AWARE. When the queue is full, the oldest EVICTABLE tick
    // is dropped to make room — a newer quote supersedes an older one, so
    // dropping the stale one is the right loss. Unevictable ticks (anything
    // carrying an abnormal trade_status, i.e. how a halt reaches the mapper) are
    // never dropped to make room, and a full queue of them is allowed to grow
    // past capacity rather than lose a CRITICAL signal (§4.4).
    //
    // Every drop is reported through the sink installed by set_drop_logger(),
    // never silently.
    void Push(LongbridgeTick tick);

    // Consumer side, called only from the single ingest thread. Blocks until a
    // tick is available or the queue is closed and drained; returns false in
    // the latter case, which is how the ingest loop terminates.
    bool Pop(LongbridgeTick* out);

    // Wakes the consumer and makes every subsequent Pop drain-then-fail.
    void Close();

    [[nodiscard]] bool closed() const;
    [[nodiscard]] Stats stats() const;
    [[nodiscard]] std::size_t size() const;

    // Installs the sink drop notices are written to. Called before any producer
    // starts. The callback runs on the producer thread with no lock held.
    using DropLogger = std::function<void(const std::string&)>;
    void set_drop_logger(DropLogger logger) { drop_logger_ = std::move(logger); }

   private:
    // Drop notices are rate-limited so a sustained overflow cannot itself
    // become the bottleneck: the first drop always reports, then every 100th.
    // A shutdown summary reports the exact totals regardless.
    static constexpr std::int64_t kDropLogEvery = 100;

    mutable std::mutex mutex_;
    std::condition_variable not_empty_;
    std::deque<LongbridgeTick> queue_;
    const std::size_t capacity_;
    bool closed_ = false;
    Stats stats_;
    DropLogger drop_logger_;
};

}  // namespace tevnnis::md
