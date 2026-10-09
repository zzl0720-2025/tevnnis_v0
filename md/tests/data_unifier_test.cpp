// DataUnifier: normalization, sector classification, universe filtering,
// dedup routing and the compact sector snapshots.

#include <catch2/catch_approx.hpp>
#include <catch2/catch_test_macros.hpp>

#include "md/data_unifier.hpp"
#include "test_support.hpp"

using Catch::Approx;
using tevnnis::md::DataUnifier;
using tevnnis::md::PipelineConfig;
using tevnnis::md::SourceStatus;
using tevnnis::md::test::BaseConfig;
using tevnnis::md::test::kBaseTs;
using tevnnis::md::test::kMinuteMs;
using tevnnis::md::test::News;
using tevnnis::md::test::Quote;
using tevnnis::md::test::Status;

TEST_CASE("DataUnifier computes change_pct and intraday extremes in C++", "[md][unifier]") {
    const PipelineConfig config = BaseConfig();
    DataUnifier unifier(config);

    unifier.Unify(Quote("NVDA.US", 103.0, 100.0, kBaseTs, 1000), kBaseTs);
    unifier.Unify(Quote("NVDA.US", 98.0, 100.0, kBaseTs + kMinuteMs, 2000), kBaseTs + kMinuteMs);
    const DataUnifier::Result result =
        unifier.Unify(Quote("NVDA.US", 105.5, 100.0, kBaseTs + 2 * kMinuteMs, 3000),
                      kBaseTs + 2 * kMinuteMs);

    REQUIRE(result.outcome == DataUnifier::Outcome::kEvent);
    const tevnnis::MarketEvent& event = *result.event;
    REQUIRE(event.type() == tevnnis::QUOTE_MOVE);
    REQUIRE(event.symbol() == "NVDA.US");
    REQUIRE(event.sector() == "Semiconductor");
    REQUIRE(event.event_id() == "quote:NVDA.US:" + std::to_string(kBaseTs + 2 * kMinuteMs));
    REQUIRE(event.event_ts() == kBaseTs + 2 * kMinuteMs);
    REQUIRE(event.ingest_ts() == kBaseTs + 2 * kMinuteMs);
    REQUIRE(event.quote().change_pct() == Approx(5.5));
    REQUIRE(event.quote().intraday_high() == Approx(105.5));
    REQUIRE(event.quote().intraday_low() == Approx(98.0));
    REQUIRE(event.quote().volume() == 3000);
    REQUIRE(event.quote().trigger().empty());  // the throttle fills this in
}

TEST_CASE("DataUnifier drops symbols outside the universe", "[md][unifier]") {
    const PipelineConfig config = BaseConfig();
    DataUnifier unifier(config);

    REQUIRE(unifier.Unify(Quote("TSLA.US", 110.0, 100.0, kBaseTs), kBaseTs).outcome ==
            DataUnifier::Outcome::kOutsideUniverse);
    REQUIRE(unifier.Unify(News("n-1", "Biotech announces merger", kBaseTs, {"BIIB.US"}), kBaseTs)
                .outcome == DataUnifier::Outcome::kOutsideUniverse);
    REQUIRE(unifier.Unify(Status("TSLA.US", SourceStatus::kHalted, kBaseTs), kBaseTs).outcome ==
            DataUnifier::Outcome::kOutsideUniverse);
    // A zero prev_close cannot produce a change_pct.
    REQUIRE(unifier.Unify(Quote("NVDA.US", 110.0, 0.0, kBaseTs), kBaseTs).outcome ==
            DataUnifier::Outcome::kInvalid);
}

TEST_CASE("DataUnifier picks the first universe symbol for multi-symbol news",
          "[md][unifier]") {
    const PipelineConfig config = BaseConfig();
    DataUnifier unifier(config);

    // Universe order is Semiconductor, Energy, Web: so NVDA wins over META,
    // and the full related list is preserved for core (v0: no fan-out).
    const DataUnifier::Result result = unifier.Unify(
        News("n-1", "Chip supply deal reshapes ad hardware", kBaseTs, {"META.US", "NVDA.US"}),
        kBaseTs);

    REQUIRE(result.outcome == DataUnifier::Outcome::kEvent);
    REQUIRE(result.event->symbol() == "NVDA.US");
    REQUIRE(result.event->sector() == "Semiconductor");
    REQUIRE(result.event->event_id() == "news:n-1");
    REQUIRE(result.event->news().related_symbols_size() == 2);
    REQUIRE(result.event->news().related_symbols(0) == "META.US");
    REQUIRE(result.event->news().related_symbols(1) == "NVDA.US");
    REQUIRE(result.event->news().url() == "https://example.com/x");
}

TEST_CASE("DataUnifier truncates news titles to the configured length", "[md][unifier]") {
    PipelineConfig config = BaseConfig();
    config.news_title_max_chars = 10;
    DataUnifier unifier(config);

    const DataUnifier::Result result =
        unifier.Unify(News("n-1", "0123456789ABCDEF", kBaseTs, {"NVDA.US"}), kBaseTs);

    REQUIRE(result.outcome == DataUnifier::Outcome::kEvent);
    REQUIRE(result.event->news().title() == "0123456789");
}

TEST_CASE("DataUnifier routes duplicate news to the dedup outcomes", "[md][unifier]") {
    const PipelineConfig config = BaseConfig();
    DataUnifier unifier(config);

    REQUIRE(unifier.Unify(News("n-1", "Nvidia raises full year revenue guidance", kBaseTs,
                               {"NVDA.US"}),
                          kBaseTs)
                .outcome == DataUnifier::Outcome::kEvent);
    REQUIRE(unifier.Unify(News("n-1", "Nvidia raises full year revenue guidance",
                               kBaseTs + kMinuteMs, {"NVDA.US"}),
                          kBaseTs + kMinuteMs)
                .outcome == DataUnifier::Outcome::kDuplicateExact);
    REQUIRE(unifier.Unify(News("n-2", "Nvidia raises full-year revenue guidance.",
                               kBaseTs + 2 * kMinuteMs, {"NVDA.US"}),
                          kBaseTs + 2 * kMinuteMs)
                .outcome == DataUnifier::Outcome::kDuplicateNear);
}

TEST_CASE("DataUnifier normalizes status events", "[md][unifier]") {
    const PipelineConfig config = BaseConfig();
    DataUnifier unifier(config);

    const DataUnifier::Result halted =
        unifier.Unify(Status("GOOGL.US", SourceStatus::kHalted, kBaseTs), kBaseTs);
    REQUIRE(halted.outcome == DataUnifier::Outcome::kEvent);
    REQUIRE(halted.event->type() == tevnnis::STATUS);
    REQUIRE(halted.event->sector() == "Web");
    REQUIRE(halted.event->status().status() == tevnnis::StatusPayload::HALTED);
    REQUIRE(halted.event->event_id() == "status:GOOGL.US:halted:" + std::to_string(kBaseTs));

    // A market-wide status has no symbol and no sector.
    const DataUnifier::Result closed =
        unifier.Unify(Status("", SourceStatus::kClosed, kBaseTs), kBaseTs);
    REQUIRE(closed.outcome == DataUnifier::Outcome::kEvent);
    REQUIRE(closed.event->symbol().empty());
    REQUIRE(closed.event->sector().empty());
    REQUIRE(closed.event->status().status() == tevnnis::StatusPayload::CLOSED);
}

TEST_CASE("DataUnifier snapshots reflect every tick, not just emitted events",
          "[md][unifier]") {
    const PipelineConfig config = BaseConfig();
    DataUnifier unifier(config);

    unifier.Unify(Quote("NVDA.US", 101.0, 100.0, kBaseTs), kBaseTs);          // quiet tick
    unifier.Unify(Quote("XOM.US", 96.5, 100.0, kBaseTs + kMinuteMs), kBaseTs);  // -3.5%

    const auto all = unifier.SnapshotsFor({});
    REQUIRE(all.size() == 2);
    REQUIRE(all[0].sector() == "Semiconductor");
    REQUIRE(all[0].symbols_size() == 1);
    REQUIRE(all[0].symbols(0).symbol() == "NVDA.US");
    REQUIRE(all[0].symbols(0).last_price() == Approx(101.0));
    REQUIRE(all[0].symbols(0).change_pct() == Approx(1.0));
    REQUIRE(all[1].sector() == "Energy");
    REQUIRE(all[1].symbols(0).change_pct() == Approx(-3.5));

    const auto energy_only = unifier.SnapshotsFor({"Energy"});
    REQUIRE(energy_only.size() == 1);
    REQUIRE(energy_only[0].sector() == "Energy");
}
