#include "md/longbridge_news_mapping.hpp"

#include <algorithm>
#include <cctype>
#include <utility>

namespace tevnnis::md {

namespace {

// Case-insensitive prefix test, for the two url schemes we accept.
bool StartsWithIgnoreCase(const std::string& text, const std::string& prefix) {
    if (text.size() < prefix.size()) {
        return false;
    }
    for (std::size_t i = 0; i < prefix.size(); ++i) {
        const auto a = static_cast<unsigned char>(text[i]);
        const auto b = static_cast<unsigned char>(prefix[i]);
        if (std::tolower(a) != std::tolower(b)) {
            return false;
        }
    }
    return true;
}

std::string ToLower(std::string text) {
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return text;
}

}  // namespace

const char* NewsMapRejectName(NewsMapReject reject) {
    switch (reject) {
        case NewsMapReject::kNone:
            return "none";
        case NewsMapReject::kEmptyId:
            return "empty_id";
        case NewsMapReject::kEmptyTitle:
            return "empty_title";
        case NewsMapReject::kInvalidTimestamp:
            return "invalid_timestamp";
        case NewsMapReject::kSuspectTimestampUnit:
            return "suspect_timestamp_unit";
        case NewsMapReject::kTooOld:
            return "too_old";
        case NewsMapReject::kAlreadySeen:
            return "already_seen";
        case NewsMapReject::kBatchTrimmed:
            return "batch_trimmed";
    }
    return "unknown";
}

std::string NewsSourceFromUrl(const std::string& url) {
    std::size_t start = 0;
    if (StartsWithIgnoreCase(url, "https://")) {
        start = 8;
    } else if (StartsWithIgnoreCase(url, "http://")) {
        start = 7;
    } else {
        return kDefaultNewsSource;
    }

    // authority ends at the first '/', '?' or '#'.
    std::size_t end = url.size();
    for (std::size_t i = start; i < url.size(); ++i) {
        const char c = url[i];
        if (c == '/' || c == '?' || c == '#') {
            end = i;
            break;
        }
    }
    std::string authority = url.substr(start, end - start);

    // Drop any userinfo ("user:pass@host") and any ":port" suffix.
    if (const std::size_t at = authority.rfind('@'); at != std::string::npos) {
        authority = authority.substr(at + 1);
    }
    if (const std::size_t colon = authority.find(':'); colon != std::string::npos) {
        authority = authority.substr(0, colon);
    }

    std::string host = ToLower(std::move(authority));
    if (StartsWithIgnoreCase(host, "www.")) {
        host = host.substr(4);
    }
    if (host.empty()) {
        return kDefaultNewsSource;
    }
    return host;
}

NewsItemMapper::NewsItemMapper() : NewsItemMapper(Options{}) {}

NewsItemMapper::NewsItemMapper(Options options) : options_(options) {
    if (options_.seen_capacity == 0) {
        options_.seen_capacity = 1;
    }
}

void NewsItemMapper::RememberSeen(const std::string& news_id) {
    if (!seen_ids_.insert(news_id).second) {
        return;  // already tracked; do not double-push into the FIFO
    }
    seen_order_.push_back(news_id);
    while (seen_order_.size() > options_.seen_capacity) {
        seen_ids_.erase(seen_order_.front());
        seen_order_.pop_front();
    }
}

NewsItemMapper::Result NewsItemMapper::Map(const std::string& symbol,
                                           const LongbridgeNewsRow& row,
                                           std::int64_t now_ms) {
    if (row.id.empty()) {
        ++counters_.rejected_empty_id;
        return Result{std::nullopt, NewsMapReject::kEmptyId};
    }
    if (row.title.empty()) {
        ++counters_.rejected_empty_title;
        return Result{std::nullopt, NewsMapReject::kEmptyTitle};
    }
    if (row.published_at_secs <= 0) {
        ++counters_.rejected_invalid_timestamp;
        return Result{std::nullopt, NewsMapReject::kInvalidTimestamp};
    }
    if (row.published_at_secs >= kMaxPlausibleNewsEpochSecs) {
        // A silent 1000x timestamp corruption would put every item outside the
        // age window forever; make it loud and named instead.
        ++counters_.rejected_suspect_timestamp_unit;
        return Result{std::nullopt, NewsMapReject::kSuspectTimestampUnit};
    }

    const std::int64_t event_ts_ms = row.published_at_secs * 1000;
    if (options_.max_age_minutes > 0) {
        const std::int64_t max_age_ms =
            static_cast<std::int64_t>(options_.max_age_minutes) * 60 * 1000;
        if (now_ms - event_ts_ms > max_age_ms) {
            ++counters_.too_old;
            return Result{std::nullopt, NewsMapReject::kTooOld};
        }
    }

    // Suppressed HERE rather than downstream on purpose: the endpoint has no
    // cursor, so every poll re-delivers the same items. Letting them reach the
    // pipeline would count each one in stats_.dedup_exact / dropped_count,
    // which are meant to mean "md collapsed a genuinely duplicated story".
    // UnifyNews's news_id + SimHash dedup remains the backstop.
    if (seen_ids_.count(row.id) != 0) {
        ++counters_.already_seen;
        return Result{std::nullopt, NewsMapReject::kAlreadySeen};
    }

    SourceEvent event;
    event.type = SourceEventType::kNews;
    event.news.news_id = row.id;
    event.news.title = row.title;  // UnifyNews truncates to news_title_max_chars (§4.2)
    event.news.source = NewsSourceFromUrl(row.url);
    event.news.url = row.url;  // §4.2: kept for reference, never sent to the LLM
    event.news.related_symbols = {symbol};  // the endpoint is per-symbol
    event.news.event_ts = event_ts_ms;
    // row.description is deliberately not copied anywhere — see the header.

    RememberSeen(row.id);
    ++counters_.mapped;
    return Result{std::move(event), NewsMapReject::kNone};
}

std::vector<SourceEvent> NewsItemMapper::MapBatch(const std::string& symbol,
                                                  std::vector<LongbridgeNewsRow> rows,
                                                  std::int64_t now_ms) {
    // Newest first. stable_sort so an API that already returns a sensible order
    // keeps it among items sharing a timestamp.
    std::stable_sort(rows.begin(), rows.end(),
                     [](const LongbridgeNewsRow& a, const LongbridgeNewsRow& b) {
                         return a.published_at_secs > b.published_at_secs;
                     });

    if (rows.size() > options_.max_items_per_symbol) {
        counters_.batch_trimmed += rows.size() - options_.max_items_per_symbol;
        rows.resize(options_.max_items_per_symbol);
    }

    std::vector<SourceEvent> events;
    events.reserve(rows.size());
    for (const LongbridgeNewsRow& row : rows) {
        Result result = Map(symbol, row, now_ms);
        if (result.news.has_value()) {
            events.push_back(std::move(*result.news));
        }
    }
    // Oldest first on the way out, so the pipeline sees news in the same
    // ascending-event_ts order a replay source would deliver.
    std::reverse(events.begin(), events.end());
    return events;
}

}  // namespace tevnnis::md
