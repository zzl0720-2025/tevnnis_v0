// QuoteThrottle: each of the four §4.4 knobs, independently, plus the v0
// CRITICAL bypass rule.

#include <catch2/catch_test_macros.hpp>

#include "md/throttle.hpp"
#include "test_support.hpp"

using tevnnis::md::PipelineConfig;
using tevnnis::md::QuoteThrottle;
using tevnnis::md::ThrottleDecision;
using tevnnis::md::test::BaseConfig;
using tevnnis::md::test::kBaseTs;
using tevnnis::md::test::kMinuteMs;

TEST_CASE("Knob 1: entry threshold X gates eligibility", "[md][throttle]") {
    const PipelineConfig config = BaseConfig();
    QuoteThrottle throttle(config);

    REQUIRE(throttle.Evaluate("NVDA.US", "Semiconductor", 2.9, kBaseTs).decision ==
            ThrottleDecision::kBelowEntryThreshold);
    REQUIRE(throttle.Evaluate("NVDA.US", "Semiconductor", -2.9, kBaseTs).decision ==
            ThrottleDecision::kBelowEntryThreshold);
    // Exactly on the threshold is eligible.
    REQUIRE(throttle.Evaluate("NVDA.US", "Semiconductor", 3.0, kBaseTs).decision ==
            ThrottleDecision::kEmit);
}

TEST_CASE("Knob 2: emit on escalation, not on in-band wobble", "[md][throttle]") {
    const PipelineConfig config = BaseConfig();
    QuoteThrottle throttle(config);

    const auto first = throttle.Evaluate("NVDA.US", "Semiconductor", 3.5, kBaseTs);
    REQUIRE(first.decision == ThrottleDecision::kEmit);
    REQUIRE(first.band_index == 1);
    REQUIRE(first.trigger == "cross_+3pct");

    // Wobble inside band 1 — in both directions — emits nothing.
    REQUIRE(throttle.Evaluate("NVDA.US", "Semiconductor", 4.2, kBaseTs + kMinuteMs).decision ==
            ThrottleDecision::kSameOrLowerBand);
    REQUIRE(throttle.Evaluate("NVDA.US", "Semiconductor", 3.1, kBaseTs + 2 * kMinuteMs)
                .decision == ThrottleDecision::kSameOrLowerBand);

    // Crossing into band 2 emits, and reports the band it crossed.
    const auto escalated =
        throttle.Evaluate("NVDA.US", "Semiconductor", 5.5, kBaseTs + 3 * kMinuteMs);
    REQUIRE(escalated.decision == ThrottleDecision::kEmit);
    REQUIRE(escalated.band_index == 2);
    REQUIRE(escalated.trigger == "cross_+5pct");

    // A downward move labels its direction.
    const auto down = throttle.Evaluate("XOM.US", "Energy", -3.5, kBaseTs);
    REQUIRE(down.decision == ThrottleDecision::kEmit);
    REQUIRE(down.trigger == "cross_-3pct");
}

TEST_CASE("Knob 2 limitation: bands are sign-agnostic", "[md][throttle]") {
    const PipelineConfig config = BaseConfig();
    QuoteThrottle throttle(config);

    REQUIRE(throttle.Evaluate("NVDA.US", "Semiconductor", 4.0, kBaseTs).decision ==
            ThrottleDecision::kEmit);
    // A reversal to -4% stays inside band 1, so §4.4 read literally emits
    // nothing. Documented in throttle.hpp; a directional band would be a
    // design change, not an md implementation choice.
    REQUIRE(throttle.Evaluate("NVDA.US", "Semiconductor", -4.0, kBaseTs + kMinuteMs).decision ==
            ThrottleDecision::kSameOrLowerBand);
}

TEST_CASE("Knob 3: per-symbol cooldown T", "[md][throttle]") {
    const PipelineConfig config = BaseConfig();  // T = 15 minutes
    QuoteThrottle throttle(config);

    REQUIRE(throttle.Evaluate("XOM.US", "Energy", -3.5, kBaseTs).decision ==
            ThrottleDecision::kEmit);
    // Falling back below X ends the move and resets the current band...
    REQUIRE(throttle.Evaluate("XOM.US", "Energy", -0.5, kBaseTs + 2 * kMinuteMs).decision ==
            ThrottleDecision::kBelowEntryThreshold);
    // ...but the cooldown still suppresses a fresh same-band move.
    REQUIRE(throttle.Evaluate("XOM.US", "Energy", -3.6, kBaseTs + 5 * kMinuteMs).decision ==
            ThrottleDecision::kCooldown);
    // Once T has elapsed, the same move is allowed through again.
    REQUIRE(throttle.Evaluate("XOM.US", "Energy", -3.6, kBaseTs + 20 * kMinuteMs).decision ==
            ThrottleDecision::kEmit);
}

TEST_CASE("Knob 3: a higher band overrides the cooldown", "[md][throttle]") {
    const PipelineConfig config = BaseConfig();
    QuoteThrottle throttle(config);

    REQUIRE(throttle.Evaluate("NVDA.US", "Semiconductor", 3.5, kBaseTs).decision ==
            ThrottleDecision::kEmit);
    REQUIRE(throttle.Evaluate("NVDA.US", "Semiconductor", 5.5, kBaseTs + kMinuteMs).decision ==
            ThrottleDecision::kEmit);
}

TEST_CASE("Knob 4: sector rate cap N per rolling hour", "[md][throttle]") {
    PipelineConfig config = BaseConfig();
    config.sector_rate_cap_per_hour = 1;
    config.Finalize();
    QuoteThrottle throttle(config);

    REQUIRE(throttle.Evaluate("NVDA.US", "Semiconductor", 3.5, kBaseTs).decision ==
            ThrottleDecision::kEmit);
    // Second Semiconductor event this hour — dropped and counted.
    REQUIRE(throttle.Evaluate("AMD.US", "Semiconductor", 3.5, kBaseTs + kMinuteMs).decision ==
            ThrottleDecision::kSectorRateCap);
    // The cap is per sector: Energy is unaffected.
    REQUIRE(throttle.Evaluate("XOM.US", "Energy", -3.5, kBaseTs + 2 * kMinuteMs).decision ==
            ThrottleDecision::kEmit);
    // The window rolls: an hour later Semiconductor has capacity again.
    REQUIRE(throttle.Evaluate("AMD.US", "Semiconductor", 3.5, kBaseTs + 61 * kMinuteMs)
                .decision == ThrottleDecision::kEmit);

    REQUIRE(throttle.counters().rate_cap == 1);
    REQUIRE(throttle.counters().emitted == 3);
}

TEST_CASE("CRITICAL bypasses the rate cap without consuming it", "[md][throttle]") {
    PipelineConfig config = BaseConfig();
    config.universe[0].symbols.push_back("INTC.US");  // a third Semiconductor name
    config.sector_rate_cap_per_hour = 1;
    config.Finalize();
    QuoteThrottle throttle(config);

    // Fill the Semiconductor cap with an ordinary event.
    REQUIRE(throttle.Evaluate("NVDA.US", "Semiconductor", 3.5, kBaseTs).decision ==
            ThrottleDecision::kEmit);
    // An ordinary event now hits the cap...
    REQUIRE(throttle.Evaluate("INTC.US", "Semiconductor", 3.5, kBaseTs + kMinuteMs).decision ==
            ThrottleDecision::kSectorRateCap);
    // ...but an extreme move gets through anyway.
    const auto critical =
        throttle.Evaluate("AMD.US", "Semiconductor", 8.5, kBaseTs + 2 * kMinuteMs);
    REQUIRE(critical.decision == ThrottleDecision::kEmit);
    REQUIRE(critical.critical);
    REQUIRE(critical.trigger == "cross_+8pct");
}

TEST_CASE("CRITICAL emissions do not consume rate-cap capacity", "[md][throttle]") {
    PipelineConfig config = BaseConfig();
    config.universe[0].symbols.push_back("INTC.US");
    config.sector_rate_cap_per_hour = 2;
    config.Finalize();
    QuoteThrottle throttle(config);

    REQUIRE(throttle.Evaluate("NVDA.US", "Semiconductor", 3.5, kBaseTs).decision ==
            ThrottleDecision::kEmit);  // cap usage: 1
    REQUIRE(throttle.Evaluate("AMD.US", "Semiconductor", 8.5, kBaseTs + kMinuteMs).decision ==
            ThrottleDecision::kEmit);  // CRITICAL — not counted
    // If the CRITICAL event had consumed capacity this would be capped.
    REQUIRE(throttle.Evaluate("INTC.US", "Semiconductor", 3.5, kBaseTs + 2 * kMinuteMs)
                .decision == ThrottleDecision::kEmit);  // cap usage: 2
}

TEST_CASE("CRITICAL bypasses the cooldown but still obeys band de-dup", "[md][throttle]") {
    const PipelineConfig config = BaseConfig();
    QuoteThrottle throttle(config);

    REQUIRE(throttle.Evaluate("AMD.US", "Semiconductor", 8.5, kBaseTs).decision ==
            ThrottleDecision::kEmit);
    // A repeat inside the same band is collapsed, CRITICAL or not.
    REQUIRE(throttle.Evaluate("AMD.US", "Semiconductor", 8.7, kBaseTs + kMinuteMs).decision ==
            ThrottleDecision::kSameOrLowerBand);
    // The move ends, then comes back extreme well inside the 15-minute
    // cooldown: an ordinary move would be suppressed, a CRITICAL one is not.
    REQUIRE(throttle.Evaluate("AMD.US", "Semiconductor", 0.5, kBaseTs + 2 * kMinuteMs)
                .decision == ThrottleDecision::kBelowEntryThreshold);
    REQUIRE(throttle.Evaluate("AMD.US", "Semiconductor", 8.6, kBaseTs + 5 * kMinuteMs)
                .decision == ThrottleDecision::kEmit);
}

TEST_CASE("QuoteThrottle counts every decision for observability", "[md][throttle]") {
    const PipelineConfig config = BaseConfig();
    QuoteThrottle throttle(config);

    throttle.Evaluate("NVDA.US", "Semiconductor", 1.0, kBaseTs);
    throttle.Evaluate("NVDA.US", "Semiconductor", 3.5, kBaseTs + kMinuteMs);
    throttle.Evaluate("NVDA.US", "Semiconductor", 4.0, kBaseTs + 2 * kMinuteMs);

    REQUIRE(throttle.counters().below_threshold == 1);
    REQUIRE(throttle.counters().emitted == 1);
    REQUIRE(throttle.counters().same_band == 1);
    REQUIRE(throttle.counters().cooldown == 0);
    REQUIRE(throttle.counters().rate_cap == 0);
}
