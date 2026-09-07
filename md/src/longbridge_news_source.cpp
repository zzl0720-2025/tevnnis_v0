#include "md/longbridge_news_source.hpp"

#include <iostream>
#include <utility>

#include "md/pipeline.hpp"  // SystemClockMs

namespace tevnnis::md {

namespace {

using longport::Config;
using longport::Status;
using longport::content::ContentContext;
using longport::content::NewsItem;

// Unwraps one SDK item into our plain row. `description` is copied here and
// then deliberately dropped by the mapper — §4.2 lets no article body into the
// event stream, and the drop happens at exactly one place.
LongbridgeNewsRow ToRow(const NewsItem& item) {
    LongbridgeNewsRow row;
    row.id = item.id;
    row.title = item.title;
    row.description = item.description;
    row.url = item.url;
    row.published_at_secs = item.published_at;  // SDK unit: seconds
    return row;
}

}  // namespace

LongbridgeNewsSource::LongbridgeNewsSource(Options options)
    : options_(std::move(options)), mapper_(options_.mapping) {}

LongbridgeNewsSource::~LongbridgeNewsSource() { Stop(); }

void LongbridgeNewsSource::Connect() {
    if (options_.symbols.empty()) {
        throw LongbridgeConnectError("universe is empty - nothing to poll news for");
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

    ctx_ = std::make_unique<ContentContext>(ContentContext::create(config));

    // One probe call before the bind (§9.1). A news entitlement we do not have
    // must stop the process here, not surface as a silently empty News zone on
    // the dashboard hours later.
    const std::string& probe = options_.symbols.front();
    const AwaitResult<std::vector<NewsItem>> items = Await<std::vector<NewsItem>>(
        [this, &probe](auto callback) { ctx_->news(probe, std::move(callback)); },
        options_.call_timeout);
    if (!items.ok) {
        throw LongbridgeConnectError("news probe for " + probe + " failed: " + items.failure());
    }
    std::cout << "tevnnis-md news probe: " << probe << " returned " << items.value.size()
              << " item(s)" << std::endl;
}

bool LongbridgeNewsSource::FetchSymbol(const std::string& symbol,
                                       std::vector<LongbridgeNewsRow>* out) {
    const AwaitResult<std::vector<NewsItem>> items = Await<std::vector<NewsItem>>(
        [this, &symbol](auto callback) { ctx_->news(symbol, std::move(callback)); },
        options_.call_timeout);
    ++stats_.calls;
    if (!items.ok) {
        // Never fatal — see the FAILURE POLICY note in the header. The message
        // is already redacted by DescribeSdkStatus (§12).
        ++stats_.call_failures;
        std::cerr << "tevnnis-md: news poll for " << symbol << " failed: " << items.failure()
                  << std::endl;
        return false;
    }
    out->clear();
    out->reserve(items.value.size());
    for (const NewsItem& item : items.value) {
        out->push_back(ToRow(item));
    }
    stats_.items_received += out->size();
    return true;
}

bool LongbridgeNewsSource::PollOnce(const std::function<void(const SourceEvent&)>& on_event) {
    std::vector<LongbridgeNewsRow> rows;
    for (const std::string& symbol : options_.symbols) {
        if (stopped_.load()) {
            return false;
        }
        if (FetchSymbol(symbol, &rows)) {
            for (const SourceEvent& event : mapper_.MapBatch(symbol, rows, SystemClockMs())) {
                ++stats_.events_emitted;
                on_event(event);
            }
        }
        // Pace the calls even on failure: a failing endpoint is exactly when we
        // least want to hammer it.
        if (options_.min_call_spacing.count() > 0 && !WaitFor(options_.min_call_spacing)) {
            return false;
        }
    }
    ++stats_.cycles;
    return true;
}

void LongbridgeNewsSource::run(const std::function<void(const SourceEvent&)>& on_event) {
    if (ctx_ == nullptr) {
        throw LongbridgeConnectError("run() called before a successful Connect()");
    }

    std::cout << "tevnnis-md polling news for " << options_.symbols.size() << " symbol(s) every "
              << options_.poll_interval.count() << "s" << std::endl;

    while (!stopped_.load()) {
        if (!PollOnce(on_event)) {
            break;
        }
        if (!WaitFor(std::chrono::duration_cast<std::chrono::milliseconds>(
                options_.poll_interval))) {
            break;
        }
    }
}

bool LongbridgeNewsSource::WaitFor(std::chrono::milliseconds duration) {
    std::unique_lock<std::mutex> lock(wait_mutex_);
    // Predicate form, so a Stop() that lands between the flag check and the
    // wait is not missed and the poll interval is not slept out.
    wake_.wait_for(lock, duration, [this] { return stopped_.load(); });
    return !stopped_.load();
}

void LongbridgeNewsSource::Stop() {
    if (stopped_.exchange(true)) {
        return;
    }
    // Under the lock so a poller thread about to wait cannot miss the notify.
    {
        const std::lock_guard<std::mutex> lock(wait_mutex_);
    }
    wake_.notify_all();
}

}  // namespace tevnnis::md
