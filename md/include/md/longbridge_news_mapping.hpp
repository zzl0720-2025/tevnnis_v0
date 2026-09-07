#pragma once
// Longbridge news item -> SourceEvent: the pure mapping layer.
//
// The exact analogue of md/longbridge_mapping.hpp for quotes. Nothing here
// includes or depends on the Longbridge SDK: LongbridgeNewsRow is a plain
// mirror of longport::content::NewsItem with the SDK types already unwrapped,
// so the whole mapping is unit-testable with synthetic payloads, offline, on a
// machine with no SDK and no Rust toolchain — and it is compiled into
// tevnnis_md unconditionally. The thin SDK-facing poller that fills a
// LongbridgeNewsRow in lives in md/longbridge_news_source.hpp, which IS
// SDK-dependent and optional.
//
// THREADING: NewsItemMapper is NOT thread-safe and is not meant to be. It holds
// the seen-id window, and it is driven from exactly one thread — the news
// poller's own thread. That thread is distinct from the quote ingest thread,
// but the two never share mapper state: the quote path owns QuoteTickMapper and
// the news path owns this. They meet only at MdPipeline::OnSourceEvent, which
// is fully mutex-guarded (md/src/pipeline.cpp).
//
// Facts this mapping is built on, read out of the pinned SDK v4.3.7 tree rather
// than assumed:
//   * longport::content::ContentContext::news(symbol, callback) returns
//     std::vector<NewsItem> for ONE symbol per call, over a plain
//     GET /v1/content/{symbol}/news with no `since`/cursor/limit parameter
//     (rust/src/content/context.rs). Every poll therefore re-delivers items we
//     have already seen — which is what the seen-id window below exists for.
//   * NewsItem = { id, title, description, url, published_at, comments_count,
//     likes_count, shares_count } (cpp/include/types.hpp). There is NO source
//     field, so NewsPayload.source is derived from the url host below.
//   * `published_at` is in SECONDS: the C binding converts with Rust's
//     `.unix_timestamp()` (c/src/content_context/types.rs), the same unit as
//     the quote `timestamp`, while NewsEvent::event_ts is epoch
//     MILLIS. Hence the *1000, plus the same defensive unit guard.
//
// §4.2 — `description` is deliberately NOT carried into NewsEvent. v0 sends no
// article body in the event stream; the field is read off the wire and dropped
// here, at the boundary, so no downstream struct can ever carry it.

#include <cstddef>
#include <cstdint>
#include <deque>
#include <optional>
#include <string>
#include <unordered_set>
#include <vector>

#include "md/market_data_source.hpp"

namespace tevnnis::md {

// A raw news item, SDK types already unwrapped. Mirrors
// longport::content::NewsItem; the counts (comments/likes/shares) are not
// mirrored because nothing downstream has any use for them.
struct LongbridgeNewsRow {
    std::string id;
    std::string title;
    std::string description;  // read, then dropped — §4.2 forbids a body downstream
    std::string url;
    std::int64_t published_at_secs = 0;  // SDK unit: seconds (see header note)
};

// Why an item produced no NewsEvent.
enum class NewsMapReject {
    kNone,                  // a NewsEvent was produced
    kEmptyId,               // no news_id — the §4.2 dedup primary key is mandatory
    kEmptyTitle,            // nothing to say; title is the entire payload for v0
    kInvalidTimestamp,      // published_at <= 0
    kSuspectTimestampUnit,  // published_at looks like millis, not seconds
    kTooOld,                // outside the backfill window
    kAlreadySeen,           // this news_id was emitted by an earlier poll
    kBatchTrimmed,          // beyond max_items_per_symbol for this poll
};

const char* NewsMapRejectName(NewsMapReject reject);

// Shared with the quote mapper's guard: a `published_at` this large is not a
// plausible second-count (beyond the year 5138) and almost certainly means the
// SDK switched to milliseconds. See md/longbridge_mapping.hpp.
inline constexpr std::int64_t kMaxPlausibleNewsEpochSecs = 100000000000LL;

// Fallback NewsPayload.source when the url yields no usable host. Named rather
// than left empty so a dashboard row is never blank.
inline constexpr const char* kDefaultNewsSource = "longbridge";

// Registrable-ish host of an https/http url, lowercased, with a leading "www."
// stripped: "https://www.reuters.com/business/x?y=1" -> "reuters.com".
// Returns kDefaultNewsSource when the url has no extractable host. This is
// presentation-grade, not a public-suffix implementation: it never has to be
// exact, only stable and non-empty.
std::string NewsSourceFromUrl(const std::string& url);

class NewsItemMapper {
   public:
    struct Options {
        // Backfill bound. The endpoint has no `since`, so the FIRST poll after
        // startup would otherwise emit whatever backlog it returns for every
        // universe symbol at once. News is never CRITICAL (§4.4) and is not
        // throttled (§4.4's four knobs are quote-only), so these two knobs are
        // the ONLY bound on news volume — which is exactly why they live here,
        // in the layer that has unit tests.
        int max_age_minutes = 120;
        std::size_t max_items_per_symbol = 5;
        // Seen-id window. Bounded so a long session cannot grow without limit.
        std::size_t seen_capacity = 4096;
    };

    struct Counters {
        std::size_t mapped = 0;
        std::size_t too_old = 0;
        std::size_t already_seen = 0;
        std::size_t batch_trimmed = 0;
        std::size_t rejected_empty_id = 0;
        std::size_t rejected_empty_title = 0;
        std::size_t rejected_invalid_timestamp = 0;
        std::size_t rejected_suspect_timestamp_unit = 0;
    };

    // Two overloads rather than `Options options = {}`: a default argument
    // cannot use a nested class's member initializers from inside the
    // enclosing class definition.
    NewsItemMapper();
    explicit NewsItemMapper(Options options);

    // Maps one poll's worth of rows for one symbol.
    //
    // Order of operations is load-bearing: sort newest-first, TRIM to
    // max_items_per_symbol, then filter each survivor. Trimming before
    // filtering is what makes "the newest 5" mean the newest 5 items the API
    // returned, independent of how many of them the age window would have kept.
    //
    // `now_ms` is epoch millis; the emitted SourceEvents carry event_ts in
    // millis too.
    std::vector<SourceEvent> MapBatch(const std::string& symbol,
                                      std::vector<LongbridgeNewsRow> rows,
                                      std::int64_t now_ms);

    // Single-item mapping, exposed for tests and used by MapBatch.
    struct Result {
        std::optional<SourceEvent> news;
        NewsMapReject reject = NewsMapReject::kNone;
    };
    Result Map(const std::string& symbol, const LongbridgeNewsRow& row, std::int64_t now_ms);

    [[nodiscard]] const Counters& counters() const { return counters_; }
    [[nodiscard]] std::size_t seen_count() const { return seen_ids_.size(); }
    // Test/diagnostic helper: has this id already been emitted?
    [[nodiscard]] bool HasSeen(const std::string& news_id) const {
        return seen_ids_.count(news_id) != 0;
    }

   private:
    void RememberSeen(const std::string& news_id);

    Options options_;
    Counters counters_;
    // FIFO of seen ids, oldest at the front, mirrored by the set for lookup —
    // the same shape as DedupWindow's entries_/ids_ pair (md/dedup.hpp).
    std::deque<std::string> seen_order_;
    std::unordered_set<std::string> seen_ids_;
};

}  // namespace tevnnis::md
