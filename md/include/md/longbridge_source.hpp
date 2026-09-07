#pragma once
// LongbridgeQuoteSource — the live §3 QuoteContext adapter.
//
// Implements the SAME MarketDataSource interface as MockMarketDataSource and
// feeds the same pipeline (Data Unifier -> dedup -> throttle ->
// priority -> sector queues) completely unchanged. Read-only: it subscribes to
// quotes and never trades, and it never writes the database (core is the sole
// writer, §2.2), so there is no api_usage accounting here — quotes carry no
// per-call cost. Trading stays in the core broker adapter.
//
// SDK-dependent: compiled only with TEVNNIS_ENABLE_LONGBRIDGE=ON. The pure
// payload mapping lives in md/longbridge_mapping.hpp and is always compiled and
// always tested, with no SDK and no network.
//
// LIFECYCLE (§9.1 — any failure aborts before we start serving):
//
//   Connect()   on the main thread, BEFORE the gRPC port is bound: builds a
//               Config from the environment, creates the QuoteContext, reports
//               the quote entitlement level, and seeds prev_close for every
//               universe symbol from ONE batched snapshot call. Throws
//               ConnectError on any failure, so bad credentials or a missing
//               entitlement stop the process before it advertises readiness.
//
//   run()       on md's single ingest thread, AFTER the server is serving:
//               installs the push callback, subscribes, then loops
//               Pop -> Map -> on_event until Stop(). Returns when drained.
//
//   Stop()      from the signal handler path: unsubscribes and closes the
//               queue, which ends run()'s loop.
//
// THREADING. The SDK push callback does the minimum possible: unwrap PushQuote
// into a plain LongbridgeTick and enqueue it. All mapping and all pipeline work
// happens on the single thread that called run(). That is deliberate and
// load-bearing — longport-c uses tokio with `rt-multi-thread`, so callbacks may
// arrive on several worker threads at once, and QuoteTickMapper holds per-symbol
// state (prev_close, halt edges) that would race if mapped there. Keeping Map()
// on the ingest thread makes that state single-threaded by construction rather
// than by an assumption about SDK callback serialization.
//
// LongbridgeNewsSource adds a second optional ingest thread. It does
// not weaken the invariant above: the news poller owns its own NewsItemMapper
// and never touches QuoteTickMapper, so all per-symbol quote state remains
// single-threaded. The two threads meet only at MdPipeline::OnSourceEvent,
// which is fully mutex-guarded.

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#include <longport.hpp>

#include "md/ingress_queue.hpp"
#include "md/longbridge_await.hpp"  // LongbridgeConnectError, shared with the news source
#include "md/longbridge_mapping.hpp"
#include "md/market_data_source.hpp"

namespace tevnnis::md {

class LongbridgeQuoteSource : public MarketDataSource {
   public:
    struct Options {
        std::vector<std::string> symbols;  // the §6 universe, in authored order
        std::chrono::seconds call_timeout{20};
        std::size_t ingress_capacity = IngressQueue::kDefaultCapacity;
    };

    explicit LongbridgeQuoteSource(Options options);
    ~LongbridgeQuoteSource() override;

    LongbridgeQuoteSource(const LongbridgeQuoteSource&) = delete;
    LongbridgeQuoteSource& operator=(const LongbridgeQuoteSource&) = delete;

    // Connects and seeds prev_close. Call once, before run(), on the main
    // thread. Throws LongbridgeConnectError on failure.
    void Connect();

    // Subscribes and pumps events until Stop(). Call on the ingest thread.
    void run(const std::function<void(const SourceEvent&)>& on_event) override;

    // Safe to call from any thread, including after run() has returned.
    void Stop();

    [[nodiscard]] const std::string& quote_level() const { return quote_level_; }
    // Universe symbols that produced no usable prev_close: every push for these
    // is rejected, so they are reported by NAME at startup, not just counted.
    [[nodiscard]] const std::vector<std::string>& unseeded_symbols() const {
        return unseeded_symbols_;
    }
    [[nodiscard]] IngressQueue::Stats ingress_stats() const { return ingress_->stats(); }
    [[nodiscard]] const QuoteTickMapper::Counters& map_counters() const {
        return mapper_.counters();
    }

   private:
    void SeedPrevClose();
    // Logs the first live push with its timestamp decoded BOTH ways. The
    // The quote smoke proved the SNAPSHOT timestamp is in seconds; this is the
    // PUSH field the mapper actually consumes, so it is observed, not assumed.
    void ReportFirstPush(const LongbridgeTick& tick) const;

    Options options_;
    // Shared with the SDK callback so a push arriving during teardown writes
    // into a live queue rather than a destroyed member.
    std::shared_ptr<IngressQueue> ingress_;
    QuoteTickMapper mapper_;  // ingest thread only
    std::unique_ptr<longport::quote::QuoteContext> ctx_;
    std::string quote_level_;
    std::vector<std::string> unseeded_symbols_;
    std::atomic<bool> stopped_{false};
    bool first_push_seen_ = false;  // ingest thread only
};

}  // namespace tevnnis::md
