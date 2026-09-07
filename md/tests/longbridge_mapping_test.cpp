// QuoteTickMapper: the pure Longbridge-payload -> SourceEvent mapping.
//
// Synthetic payloads only — no SDK, no network. These are the rules the live
// path depends on, so they are pinned here rather than discovered in
// production: the seconds->millis conversion, prev_close seeding, the regular-
// session gate (§3) and edge-triggered halt/resume (§4.4 CRITICAL).

#include <catch2/catch_test_macros.hpp>

#include "md/longbridge_mapping.hpp"

using tevnnis::md::LongbridgeTick;
using tevnnis::md::LongbridgeTradeSession;
using tevnnis::md::LongbridgeTradeStatus;
using tevnnis::md::MapReject;
using tevnnis::md::QuoteTickMapper;
using tevnnis::md::SourceEventType;
using tevnnis::md::SourceStatus;

namespace {

// A value observed by the quote smoke: 2026-09-03T19:10:57Z in seconds.
constexpr std::int64_t kTsSecs = 1788462657;

LongbridgeTick Tick(const std::string& symbol = "NVDA.US", double last_done = 230.0) {
    LongbridgeTick tick;
    tick.symbol = symbol;
    tick.last_done = last_done;
    tick.open = 225.0;
    tick.high = 231.0;
    tick.low = 224.0;
    tick.timestamp_secs = kTsSecs;
    tick.volume = 12345;
    tick.trade_status = LongbridgeTradeStatus::kNormal;
    tick.trade_session = LongbridgeTradeSession::kIntraday;
    return tick;
}

QuoteTickMapper SeededMapper() {
    QuoteTickMapper mapper;
    mapper.SeedPrevClose("NVDA.US", 224.41);  // the live prev_close from the smoke
    return mapper;
}

}  // namespace

TEST_CASE("A normal intraday tick maps to a QuoteEvent", "[md][longbridge][mapping]") {
    QuoteTickMapper mapper = SeededMapper();
    const QuoteTickMapper::Result result = mapper.Map(Tick());

    REQUIRE(result.reject == MapReject::kNone);
    REQUIRE(result.quote.has_value());
    REQUIRE_FALSE(result.status.has_value());
    REQUIRE(result.quote->type == SourceEventType::kQuote);
    REQUIRE(result.quote->quote.symbol == "NVDA.US");
    REQUIRE(result.quote->quote.last_price == 230.0);
    REQUIRE(result.quote->quote.prev_close == 224.41);
    REQUIRE(result.quote->quote.volume == 12345);
    REQUIRE(mapper.counters().mapped_quotes == 1);
}

TEST_CASE("SDK seconds become QuoteEvent millis", "[md][longbridge][mapping]") {
    // The one conversion a silent mistake would corrupt every event ordering
    // with. Verified live: 1788462657 decodes as 2026-09-03T19:10:57Z.
    QuoteTickMapper mapper = SeededMapper();
    const QuoteTickMapper::Result result = mapper.Map(Tick());

    REQUIRE(result.quote->quote.event_ts == kTsSecs * 1000);
    REQUIRE(result.quote->quote.event_ts == 1788462657000LL);
}

TEST_CASE("A millis-shaped timestamp is refused, not silently scaled",
          "[md][longbridge][mapping]") {
    QuoteTickMapper mapper = SeededMapper();
    LongbridgeTick tick = Tick();
    tick.timestamp_secs = kTsSecs * 1000;  // as if the SDK switched units

    const QuoteTickMapper::Result result = mapper.Map(tick);
    REQUIRE(result.reject == MapReject::kSuspectTimestampUnit);
    REQUIRE_FALSE(result.quote.has_value());
    REQUIRE(mapper.counters().rejected_suspect_timestamp_unit == 1);
}

TEST_CASE("A non-positive timestamp is rejected", "[md][longbridge][mapping]") {
    QuoteTickMapper mapper = SeededMapper();
    LongbridgeTick tick = Tick();
    tick.timestamp_secs = 0;
    REQUIRE(mapper.Map(tick).reject == MapReject::kInvalidTimestamp);
}

TEST_CASE("An unseeded symbol is rejected rather than passed on",
          "[md][longbridge][mapping]") {
    // DataUnifier would call this "invalid" and drop it without explanation;
    // rejecting here names the actual cause.
    QuoteTickMapper mapper;  // nothing seeded
    const QuoteTickMapper::Result result = mapper.Map(Tick());

    REQUIRE(result.reject == MapReject::kNoPrevClose);
    REQUIRE_FALSE(result.quote.has_value());
    REQUIRE(mapper.counters().rejected_no_prev_close == 1);
}

TEST_CASE("A non-positive prev_close does not count as seeded",
          "[md][longbridge][mapping]") {
    QuoteTickMapper mapper;
    mapper.SeedPrevClose("NVDA.US", 0.0);
    REQUIRE_FALSE(mapper.HasPrevClose("NVDA.US"));
    mapper.SeedPrevClose("NVDA.US", -1.0);
    REQUIRE_FALSE(mapper.HasPrevClose("NVDA.US"));
    REQUIRE(mapper.seeded_count() == 0);
    REQUIRE(mapper.Map(Tick()).reject == MapReject::kNoPrevClose);

    mapper.SeedPrevClose("NVDA.US", 224.41);
    REQUIRE(mapper.HasPrevClose("NVDA.US"));
    REQUIRE(mapper.Map(Tick()).reject == MapReject::kNone);
}

TEST_CASE("An invalid price is rejected", "[md][longbridge][mapping]") {
    QuoteTickMapper mapper = SeededMapper();
    LongbridgeTick tick = Tick("NVDA.US", 0.0);
    REQUIRE(mapper.Map(tick).reject == MapReject::kInvalidPrice);
    REQUIRE(mapper.counters().rejected_invalid_price == 1);
}

TEST_CASE("Only the regular session is ingested (§3)", "[md][longbridge][mapping]") {
    QuoteTickMapper mapper = SeededMapper();
    for (const LongbridgeTradeSession session :
         {LongbridgeTradeSession::kPre, LongbridgeTradeSession::kPost,
          LongbridgeTradeSession::kOvernight}) {
        LongbridgeTick tick = Tick();
        tick.trade_session = session;
        const QuoteTickMapper::Result result = mapper.Map(tick);
        REQUIRE(result.reject == MapReject::kNonIntradaySession);
        REQUIRE_FALSE(result.quote.has_value());
        REQUIRE_FALSE(result.status.has_value());
    }
    REQUIRE(mapper.counters().rejected_non_intraday == 3);
}

TEST_CASE("A pre-market halt is not consumed by the session gate",
          "[md][longbridge][mapping]") {
    // The gate must not advance halt state, or a halt that began pre-market
    // would go unreported once the regular session opens.
    QuoteTickMapper mapper = SeededMapper();

    LongbridgeTick pre = Tick();
    pre.trade_session = LongbridgeTradeSession::kPre;
    pre.trade_status = LongbridgeTradeStatus::kHalted;
    REQUIRE(mapper.Map(pre).reject == MapReject::kNonIntradaySession);

    LongbridgeTick intraday = Tick();
    intraday.trade_status = LongbridgeTradeStatus::kHalted;
    const QuoteTickMapper::Result result = mapper.Map(intraday);
    REQUIRE(result.status.has_value());
    REQUIRE(result.status->status.status == SourceStatus::kHalted);
}

TEST_CASE("Halt and resume are edge-triggered", "[md][longbridge][mapping]") {
    QuoteTickMapper mapper = SeededMapper();

    // Trading normally: no status event.
    REQUIRE_FALSE(mapper.Map(Tick()).status.has_value());

    LongbridgeTick halted = Tick();
    halted.trade_status = LongbridgeTradeStatus::kHalted;

    // The transition emits CRITICAL-bound HALTED, and suppresses the quote:
    // a halted symbol is not trading, so its price is not new information.
    const QuoteTickMapper::Result first_halt = mapper.Map(halted);
    REQUIRE(first_halt.status.has_value());
    REQUIRE(first_halt.status->type == SourceEventType::kStatus);
    REQUIRE(first_halt.status->status.symbol == "NVDA.US");
    REQUIRE(first_halt.status->status.status == SourceStatus::kHalted);
    REQUIRE(first_halt.status->status.event_ts == kTsSecs * 1000);
    REQUIRE(first_halt.reject == MapReject::kNotTrading);
    REQUIRE_FALSE(first_halt.quote.has_value());

    // Still halted: no repeat alarm on every subsequent tick.
    REQUIRE_FALSE(mapper.Map(halted).status.has_value());
    REQUIRE_FALSE(mapper.Map(halted).status.has_value());

    // Back to normal: exactly one RESUMED, and quotes flow again.
    const QuoteTickMapper::Result resumed = mapper.Map(Tick());
    REQUIRE(resumed.status.has_value());
    REQUIRE(resumed.status->status.status == SourceStatus::kResumed);
    REQUIRE(resumed.quote.has_value());
    REQUIRE_FALSE(mapper.Map(Tick()).status.has_value());

    REQUIRE(mapper.counters().status_edges == 2);
}

TEST_CASE("A first tick that is already halted reports the halt",
          "[md][longbridge][mapping]") {
    QuoteTickMapper mapper = SeededMapper();
    LongbridgeTick halted = Tick();
    halted.trade_status = LongbridgeTradeStatus::kSuspendTrade;

    const QuoteTickMapper::Result result = mapper.Map(halted);
    REQUIRE(result.status.has_value());
    REQUIRE(result.status->status.status == SourceStatus::kHalted);
}

TEST_CASE("Moving between two abnormal statuses does not re-alarm",
          "[md][longbridge][mapping]") {
    QuoteTickMapper mapper = SeededMapper();

    LongbridgeTick halted = Tick();
    halted.trade_status = LongbridgeTradeStatus::kHalted;
    REQUIRE(mapper.Map(halted).status.has_value());

    LongbridgeTick suspended = Tick();
    suspended.trade_status = LongbridgeTradeStatus::kSuspendTrade;
    REQUIRE_FALSE(mapper.Map(suspended).status.has_value());

    LongbridgeTick fused = Tick();
    fused.trade_status = LongbridgeTradeStatus::kFuse;
    REQUIRE_FALSE(mapper.Map(fused).status.has_value());

    REQUIRE(mapper.counters().status_edges == 1);
}

TEST_CASE("Halt state is tracked per symbol", "[md][longbridge][mapping]") {
    QuoteTickMapper mapper = SeededMapper();
    mapper.SeedPrevClose("AMD.US", 150.0);

    LongbridgeTick nvda_halted = Tick("NVDA.US");
    nvda_halted.trade_status = LongbridgeTradeStatus::kHalted;
    REQUIRE(mapper.Map(nvda_halted).status.has_value());

    // AMD is unaffected by NVDA's halt.
    const QuoteTickMapper::Result amd = mapper.Map(Tick("AMD.US", 155.0));
    REQUIRE_FALSE(amd.status.has_value());
    REQUIRE(amd.quote.has_value());
}

TEST_CASE("Evictability follows trade status", "[md][longbridge][mapping]") {
    // The ingress queue's overflow policy keys off exactly this predicate.
    REQUIRE(tevnnis::md::IsEvictable(Tick()));

    LongbridgeTick halted = Tick();
    halted.trade_status = LongbridgeTradeStatus::kHalted;
    REQUIRE_FALSE(tevnnis::md::IsEvictable(halted));

    LongbridgeTick suspended = Tick();
    suspended.trade_status = LongbridgeTradeStatus::kSuspendTrade;
    REQUIRE_FALSE(tevnnis::md::IsEvictable(suspended));
}
