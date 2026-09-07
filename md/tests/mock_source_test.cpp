#include <catch2/catch_test_macros.hpp>
#include <string>
#include <vector>

#include "md/mock_source.hpp"

using tevnnis::md::MockMarketDataSource;
using tevnnis::md::MockSourceError;
using tevnnis::md::SourceEvent;
using tevnnis::md::SourceEventType;

namespace {
std::string ScenarioPath(const std::string& name) {
    return std::string(TEVNNIS_MD_SCENARIOS_DIR) + "/" + name;
}
}  // namespace

TEST_CASE("MockMarketDataSource parses the example scenario", "[md][mock_source]") {
    MockMarketDataSource source(ScenarioPath("example_basic.json"));
    REQUIRE(source.event_count() == 3);
}

TEST_CASE("MockMarketDataSource replays events in ascending event_ts order", "[md][mock_source]") {
    MockMarketDataSource source(ScenarioPath("example_basic.json"));

    std::vector<SourceEvent> replayed;
    source.run([&](const SourceEvent& ev) { replayed.push_back(ev); });

    REQUIRE(replayed.size() == 3);

    REQUIRE(replayed[0].type == SourceEventType::kQuote);
    REQUIRE(replayed[0].quote.event_ts == 1000);
    REQUIRE(replayed[0].quote.symbol == "AAPL.US");
    REQUIRE(replayed[0].quote.last_price == 150.0);
    REQUIRE(replayed[0].quote.prev_close == 145.0);
    REQUIRE(replayed[0].quote.volume == 120000);

    REQUIRE(replayed[1].type == SourceEventType::kNews);
    REQUIRE(replayed[1].news.event_ts == 2000);
    REQUIRE(replayed[1].news.news_id == "n-001");
    REQUIRE(replayed[1].news.title == "Apple unveils new product line");
    REQUIRE(replayed[1].news.related_symbols.size() == 1);
    REQUIRE(replayed[1].news.related_symbols[0] == "AAPL.US");

    REQUIRE(replayed[2].type == SourceEventType::kQuote);
    REQUIRE(replayed[2].quote.event_ts == 3000);
    REQUIRE(replayed[2].quote.last_price == 154.5);

    for (size_t i = 1; i < replayed.size(); ++i) {
        const int64_t prev_ts =
            replayed[i - 1].type == SourceEventType::kQuote ? replayed[i - 1].quote.event_ts
                                                              : replayed[i - 1].news.event_ts;
        const int64_t cur_ts = replayed[i].type == SourceEventType::kQuote
                                    ? replayed[i].quote.event_ts
                                    : replayed[i].news.event_ts;
        REQUIRE(prev_ts <= cur_ts);
    }
}

TEST_CASE("MockMarketDataSource throws on a missing scenario file", "[md][mock_source]") {
    REQUIRE_THROWS_AS(MockMarketDataSource(ScenarioPath("does_not_exist.json")), MockSourceError);
}

TEST_CASE("MockMarketDataSource parses status events", "[md][mock_source]") {
    MockMarketDataSource source(ScenarioPath("stage3_pipeline.json"));

    std::vector<SourceEvent> replayed;
    source.run([&](const SourceEvent& ev) { replayed.push_back(ev); });

    std::vector<SourceEvent> statuses;
    for (const auto& ev : replayed) {
        if (ev.type == SourceEventType::kStatus) {
            statuses.push_back(ev);
        }
    }

    REQUIRE(statuses.size() == 2);
    REQUIRE(statuses[0].status.symbol == "GOOGL.US");
    REQUIRE(statuses[0].status.status == tevnnis::md::SourceStatus::kHalted);
    REQUIRE(statuses[0].status.event_ts == 1700001860000);
    // A market-wide status carries no symbol.
    REQUIRE(statuses[1].status.symbol.empty());
    REQUIRE(statuses[1].status.status == tevnnis::md::SourceStatus::kClosed);
}
