#include <catch2/catch_test_macros.hpp>

#include "risk/engine.hpp"
#include "test_support.hpp"

using namespace tevnnis::risk;

TEST_CASE("risk plane smoke test", "[smoke]") {
    // The baseline fixture must be permissive: every other test in this suite
    // reads a reject as "the one input I broke".
    const RiskDecision decision = evaluate(test::BaselineBuy(), test::BaselineContext());
    REQUIRE(decision.allowed());
    REQUIRE(decision.rule_id == rules::kAllowed);
}
