// Priority assignment (§4.1 levels) and the narrow §4.4 CRITICAL rules.

#include <catch2/catch_test_macros.hpp>

#include "md/priority.hpp"
#include "test_support.hpp"

using tevnnis::md::AssignPriority;
using tevnnis::md::PipelineConfig;
using tevnnis::md::test::BaseConfig;

namespace {

tevnnis::MarketEvent QuoteEventWith(double change_pct) {
    tevnnis::MarketEvent event;
    event.set_type(tevnnis::QUOTE_MOVE);
    event.set_symbol("NVDA.US");
    event.set_sector("Semiconductor");
    event.mutable_quote()->set_change_pct(change_pct);
    return event;
}

tevnnis::MarketEvent StatusEventWith(const std::string& symbol,
                                     tevnnis::StatusPayload::Status status) {
    tevnnis::MarketEvent event;
    event.set_type(tevnnis::STATUS);
    event.set_symbol(symbol);
    event.mutable_status()->set_status(status);
    return event;
}

}  // namespace

TEST_CASE("Quote priority follows the escalation bands", "[md][priority]") {
    const PipelineConfig config = BaseConfig();  // bands 3/5/8, critical 8

    REQUIRE(AssignPriority(QuoteEventWith(1.0), config) == tevnnis::LOW);
    REQUIRE(AssignPriority(QuoteEventWith(3.5), config) == tevnnis::MEDIUM);
    REQUIRE(AssignPriority(QuoteEventWith(-3.5), config) == tevnnis::MEDIUM);
    REQUIRE(AssignPriority(QuoteEventWith(5.5), config) == tevnnis::HIGH);
    REQUIRE(AssignPriority(QuoteEventWith(-7.9), config) == tevnnis::HIGH);
}

TEST_CASE("An extreme intraday move is CRITICAL in either direction", "[md][priority]") {
    const PipelineConfig config = BaseConfig();

    REQUIRE(AssignPriority(QuoteEventWith(8.0), config) == tevnnis::CRITICAL);
    REQUIRE(AssignPriority(QuoteEventWith(-12.0), config) == tevnnis::CRITICAL);

    // The threshold is a knob, not a constant.
    PipelineConfig strict = BaseConfig();
    strict.critical_move_pct = 6.0;
    strict.Finalize();
    REQUIRE(AssignPriority(QuoteEventWith(6.5), strict) == tevnnis::CRITICAL);
}

TEST_CASE("Halt and resume on a universe symbol are CRITICAL", "[md][priority]") {
    const PipelineConfig config = BaseConfig();

    REQUIRE(AssignPriority(StatusEventWith("GOOGL.US", tevnnis::StatusPayload::HALTED),
                           config) == tevnnis::CRITICAL);
    REQUIRE(AssignPriority(StatusEventWith("GOOGL.US", tevnnis::StatusPayload::RESUMED),
                           config) == tevnnis::CRITICAL);
}

TEST_CASE("Session-wide status is context, not an alarm", "[md][priority]") {
    const PipelineConfig config = BaseConfig();

    REQUIRE(AssignPriority(StatusEventWith("", tevnnis::StatusPayload::CLOSED), config) ==
            tevnnis::LOW);
    REQUIRE(AssignPriority(StatusEventWith("", tevnnis::StatusPayload::PRE_MARKET), config) ==
            tevnnis::LOW);
    REQUIRE(AssignPriority(StatusEventWith("", tevnnis::StatusPayload::POST_MARKET), config) ==
            tevnnis::LOW);
    // A halt with no symbol is not a per-symbol halt either.
    REQUIRE(AssignPriority(StatusEventWith("", tevnnis::StatusPayload::HALTED), config) ==
            tevnnis::LOW);
}

TEST_CASE("News is never CRITICAL in v0", "[md][priority]") {
    const PipelineConfig config = BaseConfig();

    tevnnis::MarketEvent event;
    event.set_type(tevnnis::NEWS);
    event.set_symbol("NVDA.US");
    event.mutable_news()->set_news_id("n-1");
    event.mutable_news()->set_title("Nvidia halted after 20% collapse, says report");

    REQUIRE(AssignPriority(event, config) == tevnnis::MEDIUM);
}
