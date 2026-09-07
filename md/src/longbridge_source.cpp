#include "md/longbridge_source.hpp"

#include <algorithm>
#include <ctime>
#include <iostream>
#include <utility>

#include "md/longbridge_await.hpp"

namespace tevnnis::md {

namespace {

using longport::Config;
using longport::Status;
using longport::quote::PushQuote;
using longport::quote::QuoteContext;
using longport::quote::SecurityQuote;
using longport::quote::SubFlags;
using longport::quote::TradeSession;
using longport::quote::TradeStatus;

LongbridgeTradeStatus FromSdk(TradeStatus status) {
    switch (status) {
        case TradeStatus::Normal:
            return LongbridgeTradeStatus::kNormal;
        case TradeStatus::Halted:
            return LongbridgeTradeStatus::kHalted;
        case TradeStatus::Delisted:
            return LongbridgeTradeStatus::kDelisted;
        case TradeStatus::Fuse:
            return LongbridgeTradeStatus::kFuse;
        case TradeStatus::PrepareList:
            return LongbridgeTradeStatus::kPrepareList;
        case TradeStatus::CodeMoved:
            return LongbridgeTradeStatus::kCodeMoved;
        case TradeStatus::ToBeOpened:
            return LongbridgeTradeStatus::kToBeOpened;
        case TradeStatus::SplitStockHalts:
            return LongbridgeTradeStatus::kSplitStockHalts;
        case TradeStatus::Expired:
            return LongbridgeTradeStatus::kExpired;
        case TradeStatus::WarrantPrepareList:
            return LongbridgeTradeStatus::kWarrantPrepareList;
        case TradeStatus::SuspendTrade:
            return LongbridgeTradeStatus::kSuspendTrade;
    }
    // An unknown status is treated as "not trading" — the conservative side:
    // it can only suppress quotes and raise a halt, never invent a resume.
    return LongbridgeTradeStatus::kSuspendTrade;
}

LongbridgeTradeSession FromSdk(TradeSession session) {
    switch (session) {
        case TradeSession::Intraday:
            return LongbridgeTradeSession::kIntraday;
        case TradeSession::Pre:
            return LongbridgeTradeSession::kPre;
        case TradeSession::Post:
            return LongbridgeTradeSession::kPost;
        case TradeSession::Overnight:
            return LongbridgeTradeSession::kOvernight;
    }
    return LongbridgeTradeSession::kOvernight;  // not intraday -> ignored
}

// Unwraps an SDK push into a plain tick. This is ALL the SDK callback thread
// does; everything else happens on the ingest thread.
//
// Takes a pointer because longport::PushEvent exposes only operator-> (there is
// no operator*), so the callback hands us the payload address directly.
LongbridgeTick ToTick(const PushQuote* push) {
    LongbridgeTick tick;
    if (push == nullptr) {
        return tick;  // rejected downstream as an invalid price
    }
    tick.symbol = push->symbol;
    tick.last_done = static_cast<double>(push->last_done);
    tick.open = static_cast<double>(push->open);
    tick.high = static_cast<double>(push->high);
    tick.low = static_cast<double>(push->low);
    tick.timestamp_secs = push->timestamp;
    tick.volume = push->volume;
    tick.trade_status = FromSdk(push->trade_status);
    tick.trade_session = FromSdk(push->trade_session);
    return tick;
}

std::string FormatUtc(std::int64_t epoch_secs) {
    const auto t = static_cast<std::time_t>(epoch_secs);
    std::tm tm{};
    if (gmtime_r(&t, &tm) == nullptr) {
        return "<out of range>";
    }
    char buf[32];
    if (std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tm) == 0) {
        return "<out of range>";
    }
    return std::string(buf);
}

}  // namespace

LongbridgeQuoteSource::LongbridgeQuoteSource(Options options)
    : options_(std::move(options)),
      ingress_(std::make_shared<IngressQueue>(options_.ingress_capacity)) {
    // Backpressure is never silent (§4.4: a lost halt is a lost safety signal).
    ingress_->set_drop_logger(
        [](const std::string& notice) { std::cerr << "tevnnis-md: " << notice << std::endl; });
}

LongbridgeQuoteSource::~LongbridgeQuoteSource() { Stop(); }

void LongbridgeQuoteSource::Connect() {
    if (options_.symbols.empty()) {
        throw LongbridgeConnectError("universe is empty - nothing to subscribe to");
    }

    // Credentials are read by the SDK itself from LONGPORT_APP_KEY /
    // LONGPORT_APP_SECRET / LONGPORT_ACCESS_TOKEN. md never reads, copies,
    // stores or logs a credential value (§12).
    Status config_status;
    Config config = Config::from_apikey_env(config_status);
    if (config_status.is_err()) {
        throw LongbridgeConnectError(
            "could not build a Longbridge config from the environment (are "
            "LONGPORT_APP_KEY / LONGPORT_APP_SECRET / LONGPORT_ACCESS_TOKEN exported?): " +
            DescribeSdkStatus(config_status));
    }

    ctx_ = std::make_unique<QuoteContext>(QuoteContext::create(config));

    // §3: real-time L1/L2 may need a paid entitlement while delayed quotes are
    // free. Reporting the level makes a sparse-push surprise diagnosable.
    const AwaitResult<std::string> level = Await<std::string>(
        [this](auto callback) { ctx_->quote_level(std::move(callback)); }, options_.call_timeout);
    if (!level.ok) {
        throw LongbridgeConnectError("quote_level failed: " + level.failure());
    }
    quote_level_ = level.value;

    SeedPrevClose();
}

void LongbridgeQuoteSource::SeedPrevClose() {
    // ONE batched call for the whole universe. §3's Quote limit is 10 calls/sec
    // and this is a single call made once at startup.
    const AwaitResult<std::vector<SecurityQuote>> snapshot =
        Await<std::vector<SecurityQuote>>(
            [this](auto callback) { ctx_->quote(options_.symbols, std::move(callback)); },
            options_.call_timeout);
    if (!snapshot.ok) {
        throw LongbridgeConnectError("prev_close snapshot failed: " + snapshot.failure());
    }

    for (const SecurityQuote& quote : snapshot.value) {
        mapper_.SeedPrevClose(quote.symbol, static_cast<double>(quote.prev_close));
    }

    // A symbol with no usable prev_close has EVERY push rejected, so name it
    // rather than only counting it.
    unseeded_symbols_.clear();
    for (const std::string& symbol : options_.symbols) {
        if (!mapper_.HasPrevClose(symbol)) {
            unseeded_symbols_.push_back(symbol);
        }
    }
}

void LongbridgeQuoteSource::run(const std::function<void(const SourceEvent&)>& on_event) {
    if (ctx_ == nullptr) {
        throw LongbridgeConnectError("run() called before a successful Connect()");
    }

    // The callback captures the queue by shared_ptr, never `this`: a push that
    // races with teardown then writes into a live queue instead of a destroyed
    // member. It does no mapping and touches no per-symbol state.
    const std::shared_ptr<IngressQueue> ingress = ingress_;
    ctx_->set_on_quote(
        [ingress](auto event) { ingress->Push(ToTick(event.operator->())); });

    const AwaitResult<bool> subscribed = AwaitVoid(
        [this](auto callback) {
            ctx_->subscribe(options_.symbols, SubFlags::QUOTE(), std::move(callback));
        },
        options_.call_timeout);
    if (!subscribed.ok) {
        throw LongbridgeConnectError("subscribe failed: " + subscribed.failure());
    }
    std::cout << "tevnnis-md subscribed to " << options_.symbols.size() << " symbol(s)"
              << std::endl;

    // The ingest loop. Single-threaded by design: this is the only place
    // QuoteTickMapper and MdPipeline::OnSourceEvent are ever driven from, which
    // is what gives throttle bands, cooldowns and dedup a well-defined order.
    LongbridgeTick tick;
    while (ingress_->Pop(&tick)) {
        if (!first_push_seen_) {
            first_push_seen_ = true;
            ReportFirstPush(tick);
        }
        const QuoteTickMapper::Result mapped = mapper_.Map(tick);
        // Status first: a halt/resume is CRITICAL (§4.4) and orders ahead of
        // the price tick that carried it.
        if (mapped.status.has_value()) {
            std::cout << "tevnnis-md status: " << tick.symbol << " "
                      << (mapped.status->status.status == SourceStatus::kHalted ? "HALTED"
                                                                                : "RESUMED")
                      << std::endl;
            on_event(*mapped.status);
        }
        if (mapped.quote.has_value()) {
            on_event(*mapped.quote);
        } else if (mapped.reject == MapReject::kNoPrevClose ||
                   mapped.reject == MapReject::kSuspectTimestampUnit) {
            // Both mean every push for this symbol is being thrown away — never
            // let that be silent.
            std::cerr << "tevnnis-md: rejecting pushes for " << tick.symbol << ": "
                      << MapRejectName(mapped.reject) << std::endl;
        }
    }
}

void LongbridgeQuoteSource::ReportFirstPush(const LongbridgeTick& tick) const {
    // The quote smoke established that the SNAPSHOT (SecurityQuote) timestamp
    // is in seconds. This is the PUSH (PushQuote) field the mapper actually
    // consumes, so print it decoded both ways and let the operator confirm by
    // observation. The mapper additionally rejects a millis-shaped value
    // outright (MapReject::kSuspectTimestampUnit), so a unit change would be
    // loud rather than a silent 1000x corruption.
    std::cout << "tevnnis-md first live push: " << tick.symbol
              << " last_done=" << tick.last_done << " volume=" << tick.volume << "\n"
              << "  timestamp=" << tick.timestamp_secs << "\n"
              << "    as seconds -> " << FormatUtc(tick.timestamp_secs) << "\n"
              << "    as millis  -> " << FormatUtc(tick.timestamp_secs / 1000) << "\n"
              << "  (mapping treats it as SECONDS; the 'as seconds' line should read as now)"
              << std::endl;
}

void LongbridgeQuoteSource::Stop() {
    if (stopped_.exchange(true)) {
        return;
    }
    if (ctx_ != nullptr) {
        // Best-effort: we are shutting down either way, so a failed unsubscribe
        // is reported but never blocks teardown.
        const AwaitResult<bool> unsubscribed = AwaitVoid(
            [this](auto callback) {
                ctx_->unsubscribe(options_.symbols, SubFlags::QUOTE(), std::move(callback));
            },
            options_.call_timeout);
        if (!unsubscribed.ok) {
            std::cerr << "tevnnis-md: unsubscribe failed: " << unsubscribed.failure() << std::endl;
        }
    }
    ingress_->Close();  // ends run()'s Pop loop
}

}  // namespace tevnnis::md
