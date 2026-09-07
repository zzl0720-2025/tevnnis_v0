#pragma once
// Longbridge quote payload -> SourceEvent: the pure mapping layer.
//
// Nothing here includes or depends on the Longbridge SDK. LongbridgeTick is a
// plain mirror of the SDK's PushQuote with the SDK types already unwrapped
// (Decimal -> double, enums -> our own), so the whole mapping is unit-testable
// with synthetic payloads, offline, on a machine with no SDK and no Rust
// toolchain — and it is compiled into tevnnis_md unconditionally. The thin
// SDK-facing adapter that fills a LongbridgeTick in lives in
// md/longbridge_source.hpp, which IS SDK-dependent and optional.
//
// THREADING: QuoteTickMapper is NOT thread-safe and is not meant to be. It
// holds per-symbol mutable state (the prev_close table and the halt-edge
// table), and the SDK delivers pushes from a multi-threaded tokio runtime
// (longport-c depends on tokio with `rt-multi-thread`), so mapping inside the
// SDK callback would race. The design keeps the SDK callback down to
// "marshal the raw tick and enqueue it" and runs Map() on md's single ingest
// thread instead — which makes this state single-threaded BY CONSTRUCTION,
// with no assumption about whether the SDK happens to serialize callbacks.
//
// Facts this mapping is built on, established empirically by the step-1
// connectivity smoke against the live account rather than assumed:
//   * PushQuote carries NO prev_close; the snapshot type SecurityQuote does.
//     DataUnifier rejects a quote with prev_close <= 0 and needs it for
//     change_pct (§4.2), so prev_close is seeded per symbol from a startup
//     snapshot call and attached to every push. An unseeded symbol is rejected
//     here, loudly, rather than silently dropped downstream as "invalid".
//   * `timestamp` is in SECONDS (verified: 1788462657 -> 2026-09-03T19:10:57Z),
//     while QuoteEvent::event_ts is epoch MILLIS. Hence the *1000, plus a
//     defensive unit guard below.

#include <cstdint>
#include <optional>
#include <string>
#include <unordered_map>

#include "md/market_data_source.hpp"

namespace tevnnis::md {

// Mirrors longport::quote::TradeStatus.
enum class LongbridgeTradeStatus {
    kNormal,
    kHalted,
    kDelisted,
    kFuse,
    kPrepareList,
    kCodeMoved,
    kToBeOpened,
    kSplitStockHalts,
    kExpired,
    kWarrantPrepareList,
    kSuspendTrade,
};

// Mirrors longport::quote::TradeSession.
enum class LongbridgeTradeSession { kIntraday, kPre, kPost, kOvernight };

// A raw push tick, SDK types already unwrapped. This is what crosses the
// ingress queue from the SDK callback threads to md's ingest thread.
struct LongbridgeTick {
    std::string symbol;
    double last_done = 0.0;
    double open = 0.0;
    double high = 0.0;
    double low = 0.0;
    std::int64_t timestamp_secs = 0;  // SDK unit: seconds (see header note)
    std::int64_t volume = 0;
    LongbridgeTradeStatus trade_status = LongbridgeTradeStatus::kNormal;
    LongbridgeTradeSession trade_session = LongbridgeTradeSession::kIntraday;
};

// Anything other than Normal means the symbol is not trading normally.
inline bool IsTrading(LongbridgeTradeStatus status) {
    return status == LongbridgeTradeStatus::kNormal;
}

// May this tick be evicted when the ingress queue overflows?
//
// Only normally-trading ticks may. A tick carrying an abnormal trade_status is
// how a halt reaches the mapper, and §4.4 makes halt/resume on a universe
// symbol one of exactly three CRITICAL cases — dropping one would lose a safety
// signal. Note this is a pure function of the tick alone, deliberately: the
// producer side has no edge state (that lives on the ingest thread), and this
// rule is still sufficient. A halt tick is never dropped, so the halt edge
// always fires; and since drop-oldest always retains the newest tick, a later
// surviving Normal tick always fires the matching resume. A transient halt
// cannot be lost inside a dropped span, because its halt tick is unevictable.
inline bool IsEvictable(const LongbridgeTick& tick) { return IsTrading(tick.trade_status); }

// Why a tick produced no QuoteEvent. A status edge may still have been emitted.
enum class MapReject {
    kNone,                  // a QuoteEvent was produced
    kNonIntradaySession,    // pre/post/overnight — v0 is regular session only (§3)
    kNotTrading,            // halted/suspended: no trading, so no new price info
    kInvalidPrice,          // last_done <= 0 (e.g. before the first trade)
    kNoPrevClose,           // symbol never seeded — change_pct is uncomputable
    kInvalidTimestamp,      // timestamp <= 0
    kSuspectTimestampUnit,  // timestamp looks like millis, not seconds
};

const char* MapRejectName(MapReject reject);

// A `timestamp_secs` this large is not a plausible second-count (it is beyond
// the year 5138) and almost certainly means the SDK switched to milliseconds.
// Guarding is cheap and turns a silent 1000x timestamp corruption into a loud,
// named rejection.
inline constexpr std::int64_t kMaxPlausibleEpochSecs = 100000000000LL;

class QuoteTickMapper {
   public:
    struct Result {
        // Emitted before `quote` when both are present: a halt/resume is
        // CRITICAL (§4.4) and orders ahead of the price tick that carried it.
        std::optional<SourceEvent> status;
        std::optional<SourceEvent> quote;
        MapReject reject = MapReject::kNone;
    };

    struct Counters {
        std::int64_t mapped_quotes = 0;
        std::int64_t status_edges = 0;
        std::int64_t rejected_non_intraday = 0;
        std::int64_t rejected_not_trading = 0;
        std::int64_t rejected_invalid_price = 0;
        std::int64_t rejected_no_prev_close = 0;
        std::int64_t rejected_invalid_timestamp = 0;
        std::int64_t rejected_suspect_timestamp_unit = 0;
    };

    // Records the previous close for `symbol`, from the startup snapshot call.
    // A non-positive value is ignored, leaving the symbol unseeded.
    void SeedPrevClose(const std::string& symbol, double prev_close);

    [[nodiscard]] bool HasPrevClose(const std::string& symbol) const;
    [[nodiscard]] std::size_t seeded_count() const { return prev_close_.size(); }

    // Maps one raw tick. Deterministic; no clock, no I/O.
    Result Map(const LongbridgeTick& tick);

    [[nodiscard]] const Counters& counters() const { return counters_; }

   private:
    std::unordered_map<std::string, double> prev_close_;
    // Per-symbol halt state for edge detection. A symbol is assumed trading
    // until a tick says otherwise, so a first tick that is already halted
    // correctly registers as a halt edge. Tracked as a bool rather than the raw
    // status so a transition between two abnormal statuses (Halted ->
    // SuspendTrade) does not emit a redundant second HALTED event.
    std::unordered_map<std::string, bool> halted_;
    Counters counters_;
};

}  // namespace tevnnis::md
