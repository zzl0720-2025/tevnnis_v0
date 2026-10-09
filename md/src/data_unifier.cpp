#include "md/data_unifier.hpp"

#include <algorithm>
#include <string>
#include <unordered_set>

namespace tevnnis::md {

namespace {

// Truncates to at most `max_chars` bytes without splitting a UTF-8 sequence.
std::string Truncate(const std::string& s, int max_chars) {
    const auto limit = static_cast<std::size_t>(max_chars);
    if (s.size() <= limit) {
        return s;
    }
    std::size_t cut = limit;
    while (cut > 0 && (static_cast<unsigned char>(s[cut]) & 0xC0U) == 0x80U) {
        --cut;
    }
    return s.substr(0, cut);
}

tevnnis::StatusPayload::Status ToProtoStatus(SourceStatus status) {
    switch (status) {
        case SourceStatus::kHalted:
            return tevnnis::StatusPayload::HALTED;
        case SourceStatus::kResumed:
            return tevnnis::StatusPayload::RESUMED;
        case SourceStatus::kPreMarket:
            return tevnnis::StatusPayload::PRE_MARKET;
        case SourceStatus::kPostMarket:
            return tevnnis::StatusPayload::POST_MARKET;
        case SourceStatus::kClosed:
            return tevnnis::StatusPayload::CLOSED;
    }
    return tevnnis::StatusPayload::CLOSED;
}

std::string StatusName(SourceStatus status) {
    switch (status) {
        case SourceStatus::kHalted:
            return "halted";
        case SourceStatus::kResumed:
            return "resumed";
        case SourceStatus::kPreMarket:
            return "pre_market";
        case SourceStatus::kPostMarket:
            return "post_market";
        case SourceStatus::kClosed:
            return "closed";
    }
    return "unknown";
}

}  // namespace

DataUnifier::DataUnifier(const PipelineConfig& config)
    : config_(config),
      dedup_(static_cast<std::int64_t>(config.dedup_window_minutes) * 60 * 1000,
             config.simhash_hamming_threshold) {}

DataUnifier::Result DataUnifier::Unify(const SourceEvent& source_event,
                                       std::int64_t ingest_ts_ms) {
    switch (source_event.type) {
        case SourceEventType::kQuote:
            return UnifyQuote(source_event.quote, ingest_ts_ms);
        case SourceEventType::kNews:
            return UnifyNews(source_event.news, ingest_ts_ms);
        case SourceEventType::kStatus:
            return UnifyStatus(source_event.status, ingest_ts_ms);
    }
    return Result{Outcome::kInvalid, std::nullopt};
}

DataUnifier::Result DataUnifier::UnifyQuote(const QuoteEvent& quote,
                                            std::int64_t ingest_ts_ms) {
    const std::string* sector = config_.SectorOf(quote.symbol);
    if (sector == nullptr) {
        return Result{Outcome::kOutsideUniverse, std::nullopt};
    }
    if (quote.prev_close <= 0.0) {
        return Result{Outcome::kInvalid, std::nullopt};
    }

    // State is updated for every in-universe tick, whether or not the tick
    // survives throttling: snapshots must reflect current state.
    // v0 keeps intraday high/low for the lifetime of the process: there is no
    // session rollover in md, and a restart re-seeds them from the next tick.
    SymbolState& state = states_[quote.symbol];
    const bool first_tick = state.last_ts == 0;
    state.last_price = quote.last_price;
    state.prev_close = quote.prev_close;
    state.change_pct = (quote.last_price - quote.prev_close) / quote.prev_close * 100.0;
    state.intraday_high =
        first_tick ? quote.last_price : std::max(state.intraday_high, quote.last_price);
    state.intraday_low =
        first_tick ? quote.last_price : std::min(state.intraday_low, quote.last_price);
    state.volume = quote.volume;
    state.last_ts = quote.event_ts;

    tevnnis::MarketEvent event;
    event.set_event_id("quote:" + quote.symbol + ":" + std::to_string(quote.event_ts));
    event.set_type(tevnnis::QUOTE_MOVE);
    event.set_symbol(quote.symbol);
    event.set_sector(*sector);
    event.set_event_ts(quote.event_ts);
    event.set_ingest_ts(ingest_ts_ms);

    tevnnis::QuotePayload* payload = event.mutable_quote();
    payload->set_last_price(state.last_price);
    payload->set_prev_close(state.prev_close);
    payload->set_change_pct(state.change_pct);
    payload->set_intraday_high(state.intraday_high);
    payload->set_intraday_low(state.intraday_low);
    payload->set_volume(state.volume);
    // `trigger` is filled in by the pipeline once the throttle has decided.

    return Result{Outcome::kEvent, std::move(event)};
}

DataUnifier::Result DataUnifier::UnifyNews(const NewsEvent& news, std::int64_t ingest_ts_ms) {
    if (news.news_id.empty()) {
        return Result{Outcome::kInvalid, std::nullopt};
    }

    // v0 decision: one event per story, no fan-out. The primary symbol is the
    // first related symbol found in universe order; the full related_symbols
    // list is preserved in the payload for core.
    const std::string* primary_symbol = nullptr;
    const std::string* sector = nullptr;
    for (const auto& entry : config_.universe) {
        for (const auto& symbol : entry.symbols) {
            const bool related = std::find(news.related_symbols.begin(),
                                           news.related_symbols.end(),
                                           symbol) != news.related_symbols.end();
            if (related) {
                primary_symbol = &symbol;
                sector = &entry.sector;
                break;
            }
        }
        if (primary_symbol != nullptr) {
            break;
        }
    }
    if (primary_symbol == nullptr) {
        return Result{Outcome::kOutsideUniverse, std::nullopt};
    }

    switch (dedup_.CheckAndRecord(news.news_id, news.title, ingest_ts_ms)) {
        case DedupWindow::Verdict::kDuplicateExact:
            return Result{Outcome::kDuplicateExact, std::nullopt};
        case DedupWindow::Verdict::kDuplicateNear:
            return Result{Outcome::kDuplicateNear, std::nullopt};
        case DedupWindow::Verdict::kNew:
            break;
    }

    tevnnis::MarketEvent event;
    event.set_event_id("news:" + news.news_id);
    event.set_type(tevnnis::NEWS);
    event.set_symbol(*primary_symbol);
    event.set_sector(*sector);
    event.set_event_ts(news.event_ts);
    event.set_ingest_ts(ingest_ts_ms);

    tevnnis::NewsPayload* payload = event.mutable_news();
    payload->set_news_id(news.news_id);
    payload->set_title(Truncate(news.title, config_.news_title_max_chars));
    payload->set_source(news.source);
    payload->set_url(news.url);
    for (const auto& symbol : news.related_symbols) {
        payload->add_related_symbols(symbol);
    }

    return Result{Outcome::kEvent, std::move(event)};
}

DataUnifier::Result DataUnifier::UnifyStatus(const StatusEvent& status,
                                             std::int64_t ingest_ts_ms) {
    // A symbol-scoped status must be in the universe; a market-wide status
    // (empty symbol) carries an empty sector and bypasses the pull's sector
    // filter, since it is context for every sector.
    std::string sector;
    if (!status.symbol.empty()) {
        const std::string* found = config_.SectorOf(status.symbol);
        if (found == nullptr) {
            return Result{Outcome::kOutsideUniverse, std::nullopt};
        }
        sector = *found;
    }

    tevnnis::MarketEvent event;
    event.set_event_id("status:" + (status.symbol.empty() ? std::string("market") : status.symbol) +
                       ":" + StatusName(status.status) + ":" + std::to_string(status.event_ts));
    event.set_type(tevnnis::STATUS);
    event.set_symbol(status.symbol);
    event.set_sector(sector);
    event.set_event_ts(status.event_ts);
    event.set_ingest_ts(ingest_ts_ms);
    event.mutable_status()->set_status(ToProtoStatus(status.status));

    return Result{Outcome::kEvent, std::move(event)};
}

std::vector<tevnnis::SectorSnapshot> DataUnifier::SnapshotsFor(
    const std::vector<std::string>& sectors) const {
    const std::unordered_set<std::string> wanted(sectors.begin(), sectors.end());

    std::vector<tevnnis::SectorSnapshot> out;
    for (const auto& entry : config_.universe) {
        if (!wanted.empty() && wanted.count(entry.sector) == 0) {
            continue;
        }
        tevnnis::SectorSnapshot snapshot;
        snapshot.set_sector(entry.sector);
        for (const auto& symbol : entry.symbols) {
            const auto it = states_.find(symbol);
            if (it == states_.end()) {
                continue;  // no tick seen yet: keep the snapshot token-cheap
            }
            tevnnis::SectorSnapshot::SymbolState* state = snapshot.add_symbols();
            state->set_symbol(symbol);
            state->set_last_price(it->second.last_price);
            state->set_change_pct(it->second.change_pct);
        }
        if (snapshot.symbols_size() > 0) {
            out.push_back(std::move(snapshot));
        }
    }
    return out;
}

}  // namespace tevnnis::md
