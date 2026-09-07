// NewsItemMapper: pure Longbridge-news -> SourceEvent mapping.
//
// Synthetic payloads only — no SDK, no network. These are the rules the live
// news path depends on, so they are pinned here rather than discovered in
// production: the seconds->millis conversion, the §4.2 "no article body"
// invariant, the derived `source`, and the three bounds that exist because the
// news endpoint has no `since` cursor (age window, per-poll cap, seen-id set).

#include <string>
#include <vector>

#include <catch2/catch_test_macros.hpp>

#include "md/longbridge_news_mapping.hpp"

using tevnnis::md::kDefaultNewsSource;
using tevnnis::md::LongbridgeNewsRow;
using tevnnis::md::NewsItemMapper;
using tevnnis::md::NewsMapReject;
using tevnnis::md::NewsSourceFromUrl;
using tevnnis::md::SourceEventType;

namespace {

// A plausible recent instant, in the SDK's unit (seconds).
constexpr std::int64_t kPublishedSecs = 1788462657;  // 2026-09-03T19:10:57Z
constexpr std::int64_t kNowMs = kPublishedSecs * 1000 + 60'000;  // one minute later

LongbridgeNewsRow Row(const std::string& id = "n-1",
                      std::int64_t published_at_secs = kPublishedSecs) {
    LongbridgeNewsRow row;
    row.id = id;
    row.title = "Analyst lifts price target on data-center demand";
    row.description = "A LONG ARTICLE BODY THAT MUST NEVER LEAVE THIS STRUCT";
    row.url = "https://www.reuters.com/business/nvda-pt";
    row.published_at_secs = published_at_secs;
    return row;
}

}  // namespace

TEST_CASE("a well-formed row maps to a news SourceEvent", "[news_mapping]") {
    NewsItemMapper mapper;
    const NewsItemMapper::Result result = mapper.Map("NVDA.US", Row(), kNowMs);

    REQUIRE(result.reject == NewsMapReject::kNone);
    REQUIRE(result.news.has_value());
    CHECK(result.news->type == SourceEventType::kNews);
    CHECK(result.news->news.news_id == "n-1");
    CHECK(result.news->news.title == "Analyst lifts price target on data-center demand");
    CHECK(result.news->news.url == "https://www.reuters.com/business/nvda-pt");
    CHECK(result.news->news.related_symbols == std::vector<std::string>{"NVDA.US"});
    CHECK(mapper.counters().mapped == 1);
}

TEST_CASE("published_at seconds become event_ts millis", "[news_mapping]") {
    NewsItemMapper mapper;
    const NewsItemMapper::Result result = mapper.Map("NVDA.US", Row(), kNowMs);

    REQUIRE(result.news.has_value());
    CHECK(result.news->news.event_ts == kPublishedSecs * 1000);
}

TEST_CASE("a millis-shaped published_at is rejected, not silently scaled", "[news_mapping]") {
    // If the SDK ever switched units, every item would fall outside the age
    // window and news would go quiet with no explanation. Make it loud instead.
    NewsItemMapper mapper;
    const NewsItemMapper::Result result =
        mapper.Map("NVDA.US", Row("n-1", kPublishedSecs * 1000), kNowMs);

    CHECK(result.reject == NewsMapReject::kSuspectTimestampUnit);
    CHECK_FALSE(result.news.has_value());
    CHECK(mapper.counters().rejected_suspect_timestamp_unit == 1);
}

TEST_CASE("malformed rows are rejected by named reason", "[news_mapping]") {
    NewsItemMapper mapper;

    SECTION("no news_id — the §4.2 dedup primary key is mandatory") {
        LongbridgeNewsRow row = Row();
        row.id.clear();
        CHECK(mapper.Map("NVDA.US", row, kNowMs).reject == NewsMapReject::kEmptyId);
        CHECK(mapper.counters().rejected_empty_id == 1);
    }
    SECTION("no title — the title is the entire v0 payload") {
        LongbridgeNewsRow row = Row();
        row.title.clear();
        CHECK(mapper.Map("NVDA.US", row, kNowMs).reject == NewsMapReject::kEmptyTitle);
        CHECK(mapper.counters().rejected_empty_title == 1);
    }
    SECTION("non-positive timestamp") {
        CHECK(mapper.Map("NVDA.US", Row("n-1", 0), kNowMs).reject ==
              NewsMapReject::kInvalidTimestamp);
        CHECK(mapper.Map("NVDA.US", Row("n-2", -5), kNowMs).reject ==
              NewsMapReject::kInvalidTimestamp);
        CHECK(mapper.counters().rejected_invalid_timestamp == 2);
    }
}

TEST_CASE("the article body never reaches the emitted event (§4.2)", "[news_mapping]") {
    // NewsEvent has no body field at all, which is the real guarantee. This
    // pins it: the description is present on the input row and appears nowhere
    // in anything the mapper hands downstream.
    NewsItemMapper mapper;
    const LongbridgeNewsRow row = Row();
    REQUIRE_FALSE(row.description.empty());

    const NewsItemMapper::Result result = mapper.Map("NVDA.US", row, kNowMs);
    REQUIRE(result.news.has_value());

    const auto& news = result.news->news;
    CHECK(news.title.find(row.description) == std::string::npos);
    CHECK(news.source.find(row.description) == std::string::npos);
    CHECK(news.url.find(row.description) == std::string::npos);
    CHECK(news.news_id.find(row.description) == std::string::npos);
}

TEST_CASE("source is derived from the url host", "[news_mapping]") {
    // NewsItem carries no source field (verified against SDK v4.3.7), so it is
    // derived — and must never come back empty, or a dashboard row is blank.
    CHECK(NewsSourceFromUrl("https://www.reuters.com/business/x") == "reuters.com");
    CHECK(NewsSourceFromUrl("https://longbridge.com/news/1") == "longbridge.com");
    CHECK(NewsSourceFromUrl("https://NEWS.Example.COM/x?y=1#z") == "news.example.com");
    CHECK(NewsSourceFromUrl("https://user:pass@host.example/x") == "host.example");
    CHECK(NewsSourceFromUrl("https://host.example:8443/x") == "host.example");
    CHECK(NewsSourceFromUrl("http://plain.example") == "plain.example");

    // Anything unusable falls back to a named source, never an empty string.
    CHECK(NewsSourceFromUrl("") == kDefaultNewsSource);
    CHECK(NewsSourceFromUrl("not a url") == kDefaultNewsSource);
    CHECK(NewsSourceFromUrl("ftp://files.example/x") == kDefaultNewsSource);
    CHECK(NewsSourceFromUrl("https://") == kDefaultNewsSource);

    NewsItemMapper mapper;
    const NewsItemMapper::Result result = mapper.Map("NVDA.US", Row(), kNowMs);
    REQUIRE(result.news.has_value());
    CHECK(result.news->news.source == "reuters.com");
}

TEST_CASE("an item older than the age window is dropped", "[news_mapping]") {
    NewsItemMapper::Options options;
    options.max_age_minutes = 120;
    NewsItemMapper mapper(options);

    const std::int64_t now_ms = kPublishedSecs * 1000;
    const std::int64_t old_secs = kPublishedSecs - (121 * 60);
    const std::int64_t fresh_secs = kPublishedSecs - (119 * 60);

    CHECK(mapper.Map("NVDA.US", Row("old", old_secs), now_ms).reject == NewsMapReject::kTooOld);
    CHECK(mapper.Map("NVDA.US", Row("fresh", fresh_secs), now_ms).reject ==
          NewsMapReject::kNone);
    CHECK(mapper.counters().too_old == 1);
}

TEST_CASE("max_age_minutes = 0 disables the age filter", "[news_mapping]") {
    NewsItemMapper::Options options;
    options.max_age_minutes = 0;
    NewsItemMapper mapper(options);

    const std::int64_t ancient = kPublishedSecs - (365 * 24 * 60 * 60);
    CHECK(mapper.Map("NVDA.US", Row("ancient", ancient), kNowMs).reject ==
          NewsMapReject::kNone);
}

TEST_CASE("a batch keeps the newest max_items_per_symbol", "[news_mapping]") {
    NewsItemMapper::Options options;
    options.max_items_per_symbol = 5;
    options.max_age_minutes = 0;  // isolate the trim from the age window
    NewsItemMapper mapper(options);

    // 12 items, deliberately handed over OLDEST first so "newest 5" cannot be
    // satisfied by accident by taking the first five.
    std::vector<LongbridgeNewsRow> rows;
    for (int i = 0; i < 12; ++i) {
        rows.push_back(Row("n-" + std::to_string(i), kPublishedSecs - (12 - i) * 60));
    }

    const std::vector<tevnnis::md::SourceEvent> events =
        mapper.MapBatch("NVDA.US", rows, kNowMs);

    REQUIRE(events.size() == 5);
    CHECK(mapper.counters().batch_trimmed == 7);
    // Emitted oldest-first, so the pipeline sees ascending event_ts like a replay.
    CHECK(events.front().news.news_id == "n-7");
    CHECK(events.back().news.news_id == "n-11");
    for (std::size_t i = 1; i < events.size(); ++i) {
        CHECK(events[i - 1].news.event_ts <= events[i].news.event_ts);
    }
}

TEST_CASE("re-polling the same batch emits nothing the second time", "[news_mapping]") {
    // The endpoint has no `since` cursor, so this is the steady state: every
    // poll re-delivers what the last one already emitted. Suppressing it HERE
    // (rather than downstream) is what keeps md's dedup_exact/dropped_count
    // counters meaning "a genuinely duplicated story".
    NewsItemMapper::Options options;
    options.max_age_minutes = 0;
    NewsItemMapper mapper(options);

    std::vector<LongbridgeNewsRow> rows{Row("a"), Row("b"), Row("c")};
    CHECK(mapper.MapBatch("NVDA.US", rows, kNowMs).size() == 3);
    CHECK(mapper.MapBatch("NVDA.US", rows, kNowMs).empty());
    CHECK(mapper.counters().already_seen == 3);

    // A genuinely new item in the next poll still gets through.
    rows.push_back(Row("d"));
    const auto third = mapper.MapBatch("NVDA.US", rows, kNowMs);
    REQUIRE(third.size() == 1);
    CHECK(third.front().news.news_id == "d");
}

TEST_CASE("the seen-id window is bounded and evicts oldest first", "[news_mapping]") {
    NewsItemMapper::Options options;
    options.max_age_minutes = 0;
    options.max_items_per_symbol = 100;
    options.seen_capacity = 3;
    NewsItemMapper mapper(options);

    for (const char* id : {"a", "b", "c"}) {
        REQUIRE(mapper.Map("NVDA.US", Row(id), kNowMs).news.has_value());
    }
    CHECK(mapper.seen_count() == 3);

    // "d" evicts "a".
    REQUIRE(mapper.Map("NVDA.US", Row("d"), kNowMs).news.has_value());
    CHECK(mapper.seen_count() == 3);
    CHECK_FALSE(mapper.HasSeen("a"));
    CHECK(mapper.HasSeen("b"));
    CHECK(mapper.HasSeen("d"));

    // Having been evicted, "a" is emittable again — the bound is deliberate,
    // and UnifyNews's own news_id dedup is the backstop.
    CHECK(mapper.Map("NVDA.US", Row("a"), kNowMs).news.has_value());
}

TEST_CASE("counters account for every row in a mixed batch", "[news_mapping]") {
    NewsItemMapper::Options options;
    options.max_age_minutes = 120;
    options.max_items_per_symbol = 10;
    NewsItemMapper mapper(options);

    const std::int64_t now_ms = kPublishedSecs * 1000;
    std::vector<LongbridgeNewsRow> rows;
    rows.push_back(Row("good-1", kPublishedSecs - 60));
    rows.push_back(Row("good-2", kPublishedSecs - 120));
    rows.push_back(Row("stale", kPublishedSecs - (200 * 60)));
    rows.push_back(Row("bad-ts", 0));
    LongbridgeNewsRow no_id = Row("", kPublishedSecs);
    rows.push_back(no_id);
    LongbridgeNewsRow no_title = Row("no-title", kPublishedSecs);
    no_title.title.clear();
    rows.push_back(no_title);

    const auto events = mapper.MapBatch("NVDA.US", rows, now_ms);

    CHECK(events.size() == 2);
    const NewsItemMapper::Counters& c = mapper.counters();
    CHECK(c.mapped == 2);
    CHECK(c.too_old == 1);
    CHECK(c.rejected_invalid_timestamp == 1);
    CHECK(c.rejected_empty_id == 1);
    CHECK(c.rejected_empty_title == 1);
    CHECK(c.batch_trimmed == 0);
    // Every one of the six rows is accounted for exactly once.
    CHECK(c.mapped + c.too_old + c.rejected_invalid_timestamp + c.rejected_empty_id +
              c.rejected_empty_title + c.already_seen + c.batch_trimmed ==
          rows.size());
}

TEST_CASE("reject reasons all have names", "[news_mapping]") {
    // A counter printed at shutdown is only useful if it can be named.
    for (const NewsMapReject reject :
         {NewsMapReject::kNone, NewsMapReject::kEmptyId, NewsMapReject::kEmptyTitle,
          NewsMapReject::kInvalidTimestamp, NewsMapReject::kSuspectTimestampUnit,
          NewsMapReject::kTooOld, NewsMapReject::kAlreadySeen, NewsMapReject::kBatchTrimmed}) {
        const std::string name = tevnnis::md::NewsMapRejectName(reject);
        CHECK_FALSE(name.empty());
        CHECK(name != "unknown");
    }
}
