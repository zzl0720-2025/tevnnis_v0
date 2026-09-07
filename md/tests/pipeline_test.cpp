// MdPipeline end to end: a scenario replayed through the full plane, the §4.3
// pull filters, and the cursor contract (never skip, may re-deliver).

#include <string>
#include <vector>

#include <catch2/catch_approx.hpp>
#include <catch2/catch_test_macros.hpp>

#include "md/mock_source.hpp"
#include "md/pipeline.hpp"
#include "test_support.hpp"

using Catch::Approx;
using tevnnis::md::MdPipeline;
using tevnnis::md::MockMarketDataSource;
using tevnnis::md::PipelineConfig;
using tevnnis::md::ReplayClock;
using tevnnis::md::SourceEvent;
using tevnnis::md::SourceEventTs;
using tevnnis::md::SourceStatus;
using tevnnis::md::test::BaseConfig;
using tevnnis::md::test::kBaseTs;
using tevnnis::md::test::kMinuteMs;
using tevnnis::md::test::News;
using tevnnis::md::test::Quote;
using tevnnis::md::test::ScenarioFile;
using tevnnis::md::test::Status;

namespace {

// Replays a scenario file with the clock driven by source timestamps, so
// throttling windows advance with scenario time and the run is reproducible.
void ReplayScenario(const std::string& file, MdPipeline* pipeline, ReplayClock* clock) {
    MockMarketDataSource source(ScenarioFile(file));
    source.run([&](const SourceEvent& event) {
        clock->set_now_ms(SourceEventTs(event));
        pipeline->OnSourceEvent(event);
    });
}

tevnnis::PullRequest Request(int max_events = 0,
                             tevnnis::Priority min_priority = tevnnis::LOW,
                             const std::vector<std::string>& sectors = {},
                             const std::string& since_cursor = "") {
    tevnnis::PullRequest request;
    request.set_max_events(max_events);
    request.set_min_priority(min_priority);
    for (const auto& sector : sectors) {
        request.add_sectors(sector);
    }
    request.set_since_cursor(since_cursor);
    return request;
}

std::vector<std::string> EventIds(const tevnnis::PullResponse& response) {
    std::vector<std::string> ids;
    ids.reserve(static_cast<std::size_t>(response.events_size()));
    for (const auto& event : response.events()) {
        ids.push_back(event.event_id());
    }
    return ids;
}

}  // namespace

TEST_CASE("Scenario replay yields a deterministic prioritized batch", "[md][pipeline]") {
    ReplayClock clock;
    MdPipeline pipeline(BaseConfig(), clock.AsClock());
    ReplayScenario("stage3_pipeline.json", &pipeline, &clock);

    const tevnnis::PullResponse response = pipeline.Pull(Request());

    // Ordered by priority desc, then event_ts asc: two CRITICALs, one HIGH,
    // three MEDIUMs, one LOW. Everything else was filtered, collapsed or
    // throttled away.
    REQUIRE(EventIds(response) ==
            std::vector<std::string>{
                "quote:AMD.US:" + std::to_string(kBaseTs + 30 * kMinuteMs),  // CRITICAL +8.5%
                "status:GOOGL.US:halted:" + std::to_string(kBaseTs + 31 * kMinuteMs),
                "quote:NVDA.US:" + std::to_string(kBaseTs + 3 * kMinuteMs),  // HIGH +5.5%
                "quote:NVDA.US:" + std::to_string(kBaseTs + kMinuteMs),      // MEDIUM +3.5%
                "news:n-001",
                "quote:XOM.US:" + std::to_string(kBaseTs + 10 * kMinuteMs),  // MEDIUM -3.5%
                "status:market:closed:" + std::to_string(kBaseTs + 32 * kMinuteMs),
            });

    REQUIRE(response.events(0).priority() == tevnnis::CRITICAL);
    REQUIRE(response.events(1).priority() == tevnnis::CRITICAL);
    REQUIRE(response.events(2).priority() == tevnnis::HIGH);
    REQUIRE(response.events(2).quote().trigger() == "cross_+5pct");
    REQUIRE(response.events(3).priority() == tevnnis::MEDIUM);
    REQUIRE(response.events(3).quote().trigger() == "cross_+3pct");
    REQUIRE(response.events(5).quote().trigger() == "cross_-3pct");
    REQUIRE(response.events(6).priority() == tevnnis::LOW);

    // dropped_count covers what was collapsed or throttled since the last pull:
    // one in-band wobble, one near-duplicate, one exact duplicate, one cooldown
    // suppression. The out-of-universe news is filtered, not dropped.
    REQUIRE(response.dropped_count() == 4);

    const MdPipeline::Stats stats = pipeline.stats();
    REQUIRE(stats.ingested == 14);
    REQUIRE(stats.emitted == 7);
    REQUIRE(stats.outside_universe == 1);
    REQUIRE(stats.dedup_exact == 1);
    REQUIRE(stats.dedup_near == 1);
    REQUIRE(stats.throttled_same_band == 1);
    REQUIRE(stats.throttled_cooldown == 1);
    REQUIRE(stats.throttled_rate_cap == 0);
}

TEST_CASE("Snapshots carry current state for symbols with no event", "[md][pipeline]") {
    ReplayClock clock;
    MdPipeline pipeline(BaseConfig(), clock.AsClock());
    ReplayScenario("stage3_pipeline.json", &pipeline, &clock);

    const tevnnis::PullResponse response = pipeline.Pull(Request());

    REQUIRE(response.snapshots_size() == 2);  // Web/BroadETF saw no quote ticks
    REQUIRE(response.snapshots(0).sector() == "Semiconductor");
    REQUIRE(response.snapshots(0).symbols_size() == 2);
    REQUIRE(response.snapshots(0).symbols(0).symbol() == "NVDA.US");
    REQUIRE(response.snapshots(0).symbols(0).last_price() == Approx(105.5));
    REQUIRE(response.snapshots(0).symbols(0).change_pct() == Approx(5.5));
    REQUIRE(response.snapshots(1).sector() == "Energy");
    // XOM's last tick never became an event (cooldown) but is still reported.
    REQUIRE(response.snapshots(1).symbols(0).last_price() == Approx(96.4));
    REQUIRE(response.snapshots(1).symbols(0).change_pct() == Approx(-3.6));
}

TEST_CASE("Pull honors max_events, min_priority and sectors", "[md][pipeline]") {
    ReplayClock clock;
    MdPipeline pipeline(BaseConfig(), clock.AsClock());
    ReplayScenario("stage3_pipeline.json", &pipeline, &clock);

    SECTION("max_events takes the top N by priority") {
        const tevnnis::PullResponse response = pipeline.Pull(Request(2));
        REQUIRE(response.events_size() == 2);
        REQUIRE(response.events(0).priority() == tevnnis::CRITICAL);
        REQUIRE(response.events(1).priority() == tevnnis::CRITICAL);
    }

    SECTION("min_priority filters the tail") {
        const tevnnis::PullResponse response = pipeline.Pull(Request(0, tevnnis::HIGH));
        REQUIRE(response.events_size() == 3);
        for (const auto& event : response.events()) {
            REQUIRE(event.priority() >= tevnnis::HIGH);
        }
    }

    SECTION("sectors filter, with market-wide events always included") {
        const tevnnis::PullResponse response =
            pipeline.Pull(Request(0, tevnnis::LOW, {"Energy"}));
        REQUIRE(EventIds(response) ==
                std::vector<std::string>{
                    "quote:XOM.US:" + std::to_string(kBaseTs + 10 * kMinuteMs),
                    "status:market:closed:" + std::to_string(kBaseTs + 32 * kMinuteMs),
                });
        REQUIRE(response.snapshots_size() == 1);
        REQUIRE(response.snapshots(0).sector() == "Energy");
    }
}

TEST_CASE("since_cursor does not re-deliver an exhausted batch", "[md][pipeline]") {
    ReplayClock clock;
    MdPipeline pipeline(BaseConfig(), clock.AsClock());
    ReplayScenario("stage3_pipeline.json", &pipeline, &clock);

    const tevnnis::PullResponse first = pipeline.Pull(Request());
    REQUIRE(first.events_size() == 7);
    REQUIRE_FALSE(first.next_cursor().empty());

    const tevnnis::PullResponse second = pipeline.Pull(Request(0, tevnnis::LOW, {},
                                                              first.next_cursor()));
    REQUIRE(second.events_size() == 0);
    REQUIRE(second.next_cursor() == first.next_cursor());
    REQUIRE(second.dropped_count() == 0);  // reset by the first pull
    // Snapshots are state, not deltas: they are sent every round.
    REQUIRE(second.snapshots_size() == 2);

    // A new event after the cursor is delivered on its own.
    clock.set_now_ms(kBaseTs + 40 * kMinuteMs);
    pipeline.OnSourceEvent(Quote("META.US", 104.0, 100.0, kBaseTs + 40 * kMinuteMs));
    const tevnnis::PullResponse third =
        pipeline.Pull(Request(0, tevnnis::LOW, {}, second.next_cursor()));
    REQUIRE(EventIds(third) ==
            std::vector<std::string>{"quote:META.US:" + std::to_string(kBaseTs + 40 * kMinuteMs)});
}

TEST_CASE("A truncated batch never skips, but may re-deliver", "[md][pipeline]") {
    ReplayClock clock;
    MdPipeline pipeline(BaseConfig(), clock.AsClock());

    // Enqueue a MEDIUM first, then a CRITICAL: priority order and sequence
    // order disagree, which is exactly when truncation re-delivers.
    clock.set_now_ms(kBaseTs);
    REQUIRE(pipeline.OnSourceEvent(Quote("NVDA.US", 103.5, 100.0, kBaseTs)));  // seq 1, MEDIUM
    clock.set_now_ms(kBaseTs + kMinuteMs);
    REQUIRE(pipeline.OnSourceEvent(
        Quote("AMD.US", 109.0, 100.0, kBaseTs + kMinuteMs)));  // seq 2, CRITICAL

    const tevnnis::PullResponse first = pipeline.Pull(Request(1));
    REQUIRE(EventIds(first) ==
            std::vector<std::string>{"quote:AMD.US:" + std::to_string(kBaseTs + kMinuteMs)});

    // The MEDIUM (seq 1) was left behind, so the cursor is held below it. The
    // already-delivered CRITICAL (seq 2) therefore comes back — deliberate:
    // never skipping matters, re-delivering is core's event_id constraint's job.
    const tevnnis::PullResponse second =
        pipeline.Pull(Request(0, tevnnis::LOW, {}, first.next_cursor()));
    REQUIRE(EventIds(second) ==
            std::vector<std::string>{
                "quote:AMD.US:" + std::to_string(kBaseTs + kMinuteMs),
                "quote:NVDA.US:" + std::to_string(kBaseTs),
            });

    // Once nothing is left behind the cursor advances past both.
    const tevnnis::PullResponse third =
        pipeline.Pull(Request(0, tevnnis::LOW, {}, second.next_cursor()));
    REQUIRE(third.events_size() == 0);
}

TEST_CASE("A cursor from a previous md process serves the current buffer",
          "[md][pipeline]") {
    ReplayClock clock;
    MdPipeline pipeline(BaseConfig(), clock.AsClock(), "epoch-current");
    ReplayScenario("stage3_pipeline.json", &pipeline, &clock);

    // md persists no delivery state, so after an md restart core's stored
    // cursor names an epoch this process knows nothing about. The buffer is
    // served in full and core's event_id unique constraint absorbs the repeats.
    for (const std::string& stale : {"epoch-previous:5", "not-a-cursor", "epoch-current:abc"}) {
        const tevnnis::PullResponse response =
            pipeline.Pull(Request(0, tevnnis::LOW, {}, stale));
        REQUIRE(response.events_size() == 7);
    }

    // A cursor from this process is still honored.
    const tevnnis::PullResponse fresh = pipeline.Pull(Request());
    REQUIRE(pipeline
                .Pull(Request(0, tevnnis::LOW, {}, fresh.next_cursor()))
                .events_size() == 0);
}

TEST_CASE("The retain buffer bounds memory and counts what it drops",
          "[md][pipeline]") {
    PipelineConfig config = BaseConfig();
    config.retain_buffer_capacity = 2;
    config.cooldown_minutes = 0;
    config.Finalize();

    ReplayClock clock;
    MdPipeline pipeline(config, clock.AsClock());

    // Three separate news items, each of which becomes an event.
    const std::vector<std::string> titles = {"Nvidia opens a new fabrication campus",
                                             "Exxon signs a long term supply agreement",
                                             "Alphabet reorganizes its advertising division"};
    const std::vector<std::vector<std::string>> symbols = {
        {"NVDA.US"}, {"XOM.US"}, {"GOOGL.US"}};
    for (std::size_t i = 0; i < titles.size(); ++i) {
        const std::int64_t ts = kBaseTs + static_cast<std::int64_t>(i) * kMinuteMs;
        clock.set_now_ms(ts);
        REQUIRE(pipeline.OnSourceEvent(News("n-" + std::to_string(i), titles[i], ts, symbols[i])));
    }

    const tevnnis::PullResponse response = pipeline.Pull(Request());
    REQUIRE(response.events_size() == 2);  // the oldest aged out
    REQUIRE(EventIds(response) == std::vector<std::string>{"news:n-1", "news:n-2"});
    REQUIRE(response.dropped_count() == 1);
    REQUIRE(pipeline.stats().evicted == 1);
}
