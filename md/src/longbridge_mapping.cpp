#include "md/longbridge_mapping.hpp"

namespace tevnnis::md {

const char* MapRejectName(MapReject reject) {
    switch (reject) {
        case MapReject::kNone:
            return "none";
        case MapReject::kNonIntradaySession:
            return "non_intraday_session";
        case MapReject::kNotTrading:
            return "not_trading";
        case MapReject::kInvalidPrice:
            return "invalid_price";
        case MapReject::kNoPrevClose:
            return "no_prev_close";
        case MapReject::kInvalidTimestamp:
            return "invalid_timestamp";
        case MapReject::kSuspectTimestampUnit:
            return "suspect_timestamp_unit";
    }
    return "unknown";
}

void QuoteTickMapper::SeedPrevClose(const std::string& symbol, double prev_close) {
    if (prev_close <= 0.0) {
        return;  // unusable — leave the symbol unseeded so Map() rejects loudly
    }
    prev_close_[symbol] = prev_close;
}

bool QuoteTickMapper::HasPrevClose(const std::string& symbol) const {
    return prev_close_.find(symbol) != prev_close_.end();
}

QuoteTickMapper::Result QuoteTickMapper::Map(const LongbridgeTick& tick) {
    Result result;

    // Gate 1 — session. v0 trades the regular session only (§3: single market,
    // 09:30-16:00 ET; the paper account has no pre/post support anyway), so a
    // non-intraday tick is discarded whole. State is deliberately NOT advanced:
    // a halt that begins pre-market is still an unseen halt when the regular
    // session opens, and the first intraday tick must report it.
    if (tick.trade_session != LongbridgeTradeSession::kIntraday) {
        ++counters_.rejected_non_intraday;
        result.reject = MapReject::kNonIntradaySession;
        return result;
    }

    // Gate 2 — halt/resume edge (§4.4 CRITICAL). Edge-triggered: only a change
    // emits, so a symbol sitting halted does not re-alarm on every tick.
    const bool now_halted = !IsTrading(tick.trade_status);
    const auto halted_it = halted_.find(tick.symbol);
    const bool was_halted = halted_it != halted_.end() && halted_it->second;
    if (now_halted != was_halted) {
        halted_[tick.symbol] = now_halted;
        SourceEvent status_event;
        status_event.type = SourceEventType::kStatus;
        status_event.status.symbol = tick.symbol;
        status_event.status.status = now_halted ? SourceStatus::kHalted : SourceStatus::kResumed;
        status_event.status.event_ts = tick.timestamp_secs * 1000;
        result.status = std::move(status_event);
        ++counters_.status_edges;
    }

    // Gate 3 — a halted symbol is not trading, so its repeated last_done is not
    // new market information. The CRITICAL status event above is the signal;
    // letting the price through would also freeze bogus intraday high/low and
    // snapshot state for the duration of the halt.
    if (now_halted) {
        ++counters_.rejected_not_trading;
        result.reject = MapReject::kNotTrading;
        return result;
    }

    if (tick.last_done <= 0.0) {
        ++counters_.rejected_invalid_price;
        result.reject = MapReject::kInvalidPrice;
        return result;
    }

    if (tick.timestamp_secs <= 0) {
        ++counters_.rejected_invalid_timestamp;
        result.reject = MapReject::kInvalidTimestamp;
        return result;
    }
    if (tick.timestamp_secs > kMaxPlausibleEpochSecs) {
        ++counters_.rejected_suspect_timestamp_unit;
        result.reject = MapReject::kSuspectTimestampUnit;
        return result;
    }

    // Gate 4 — prev_close. Not in the push; seeded from the startup snapshot.
    const auto prev_it = prev_close_.find(tick.symbol);
    if (prev_it == prev_close_.end()) {
        ++counters_.rejected_no_prev_close;
        result.reject = MapReject::kNoPrevClose;
        return result;
    }

    SourceEvent quote_event;
    quote_event.type = SourceEventType::kQuote;
    quote_event.quote.symbol = tick.symbol;
    quote_event.quote.last_price = tick.last_done;
    quote_event.quote.prev_close = prev_it->second;
    quote_event.quote.volume = tick.volume;
    // SDK seconds -> QuoteEvent epoch millis.
    quote_event.quote.event_ts = tick.timestamp_secs * 1000;
    result.quote = std::move(quote_event);
    ++counters_.mapped_quotes;

    // Intraday high/low and change_pct are DataUnifier's job (§4.2: computed in
    // C++, never by the LLM) — it already tracks them per symbol from every
    // tick. The mapper deliberately does not duplicate that state.
    return result;
}

}  // namespace tevnnis::md
