#pragma once
// LongbridgeNewsSource — the live §4.2 news adapter.
//
// The news counterpart to LongbridgeQuoteSource: it implements the
// SAME MarketDataSource interface as MockMarketDataSource and feeds the SAME
// existing pipeline (Data Unifier -> dedup -> priority -> sector queues)
// completely unchanged. Read-only: it reads a news list and never trades, and
// it never writes the database (core is the sole writer, §2.2).
//
// SDK-dependent: compiled only with TEVNNIS_ENABLE_LONGBRIDGE=ON. The pure
// payload mapping lives in md/longbridge_news_mapping.hpp and is always
// compiled and always tested, with no SDK and no network.
//
// PULL, NOT PUSH. Unlike quotes, Longbridge exposes no news subscription: the
// only entry point is longport::content::ContentContext::news(symbol), a plain
// GET /v1/content/{symbol}/news taking ONE symbol per call with no `since`
// cursor. So this source is a poller, not a stream, and it carries three
// consequences the quote source does not have:
//
//   * cadence is ours to choose  — one full cycle over the universe every
//     poll_interval (default 300s, matching cadence.decision_interval_seconds),
//     with min_call_spacing between per-symbol calls so a large universe never
//     bursts against §3's rate limits;
//   * every poll re-delivers items we already emitted — suppressed by the
//     mapper's seen-news_id window, BEFORE the pipeline, so md's dedup and
//     dropped_count counters keep meaning "a genuinely duplicated story";
//   * the first poll would otherwise dump each symbol's whole backlog at once —
//     bounded by the mapper's max_age_minutes + max_items_per_symbol.
//
// LIFECYCLE (§9.1 — any failure aborts before we start serving):
//
//   Connect()   on the main thread, BEFORE the gRPC port is bound: builds a
//               Config from the environment, creates the ContentContext, and
//               makes ONE probe call for the first universe symbol. Throws
//               LongbridgeConnectError on any failure, so bad credentials or a
//               missing entitlement stop the process before it advertises
//               readiness.
//
//   run()       on its own poller thread, AFTER the server is serving: loops
//               poll-cycle -> wait -> poll-cycle until Stop().
//
//   Stop()      from the signal handler path: sets the flag and wakes the
//               inter-cycle wait, so Ctrl-C is never delayed by a poll interval.
//
// THREADING. This is md's second ingest thread. It owns its NewsItemMapper
// outright and touches no quote state, so LongbridgeQuoteSource's "per-symbol
// state is single-threaded by construction" invariant is untouched. Both
// threads call MdPipeline::OnSourceEvent, which is fully mutex-guarded.
//
// FAILURE POLICY. A failed poll for one symbol is logged (redacted, §12),
// counted, and skipped — never fatal. This is a deliberate asymmetry with the
// quote path: a lost halt tick is a lost safety signal, whereas missing news is
// only missing context, and news is never CRITICAL (§4.4). A source that
// aborted md on a transient news 500 would trade reliability for nothing.

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include <longport.hpp>

#include "md/longbridge_await.hpp"
#include "md/longbridge_news_mapping.hpp"
#include "md/market_data_source.hpp"

namespace tevnnis::md {

class LongbridgeNewsSource : public MarketDataSource {
   public:
    struct Options {
        std::vector<std::string> symbols;  // the §6 universe, in authored order
        std::chrono::seconds poll_interval{300};
        std::chrono::milliseconds min_call_spacing{250};
        std::chrono::seconds call_timeout{20};
        NewsItemMapper::Options mapping;
    };

    struct Stats {
        std::size_t cycles = 0;
        std::size_t calls = 0;
        std::size_t call_failures = 0;
        std::size_t items_received = 0;
        std::size_t events_emitted = 0;
    };

    explicit LongbridgeNewsSource(Options options);
    ~LongbridgeNewsSource() override;

    LongbridgeNewsSource(const LongbridgeNewsSource&) = delete;
    LongbridgeNewsSource& operator=(const LongbridgeNewsSource&) = delete;

    // Connects and probes. Call once, before run(), on the main thread.
    // Throws LongbridgeConnectError on failure.
    void Connect();

    // Polls until Stop(). Call on the news poller thread.
    void run(const std::function<void(const SourceEvent&)>& on_event) override;

    // Safe to call from any thread, including after run() has returned.
    void Stop();

    [[nodiscard]] Stats stats() const { return stats_; }
    [[nodiscard]] const NewsItemMapper::Counters& map_counters() const {
        return mapper_.counters();
    }

   private:
    // One pass over the universe. Returns false if Stop() interrupted it.
    bool PollOnce(const std::function<void(const SourceEvent&)>& on_event);
    // Fetches one symbol's list; returns false and logs (redacted) on failure.
    bool FetchSymbol(const std::string& symbol, std::vector<LongbridgeNewsRow>* out);
    // Interruptible sleep; returns false if Stop() fired while waiting.
    bool WaitFor(std::chrono::milliseconds duration);

    Options options_;
    NewsItemMapper mapper_;  // poller thread only
    std::unique_ptr<longport::content::ContentContext> ctx_;
    Stats stats_;  // poller thread only, read after join
    std::atomic<bool> stopped_{false};
    std::mutex wait_mutex_;
    std::condition_variable wake_;
};

}  // namespace tevnnis::md
