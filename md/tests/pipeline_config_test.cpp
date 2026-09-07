// PipelineConfig: universe indexing, validation, and the demo JSON loader.

#include <catch2/catch_test_macros.hpp>

#include "md/pipeline_config.hpp"
#include "test_support.hpp"

using tevnnis::md::ConfigError;
using tevnnis::md::LoadPipelineConfigFromJson;
using tevnnis::md::PipelineConfig;
using tevnnis::md::test::BaseConfig;
using tevnnis::md::test::ScenarioFile;

TEST_CASE("PipelineConfig indexes symbols to sectors", "[md][config]") {
    const PipelineConfig config = BaseConfig();

    REQUIRE(*config.SectorOf("NVDA.US") == "Semiconductor");
    REQUIRE(*config.SectorOf("XOM.US") == "Energy");
    REQUIRE(config.SectorOf("BIIB.US") == nullptr);
    REQUIRE(config.InUniverse("SPY.US"));
    REQUIRE_FALSE(config.InUniverse("TSLA.US"));
    REQUIRE(config.Sectors() ==
            std::vector<std::string>{"Semiconductor", "Energy", "Web", "BroadETF"});
}

TEST_CASE("PipelineConfig rejects invalid configurations", "[md][config]") {
    SECTION("empty universe") {
        PipelineConfig config;
        config.universe.clear();
        REQUIRE_THROWS_AS(config.Finalize(), ConfigError);
    }
    SECTION("symbol in two sectors") {
        PipelineConfig config = BaseConfig();
        config.universe.push_back({"Duplicate", {"NVDA.US"}});
        REQUIRE_THROWS_AS(config.Finalize(), ConfigError);
    }
    SECTION("non-ascending bands") {
        PipelineConfig config = BaseConfig();
        config.escalation_bands = {3.0, 3.0, 8.0};
        REQUIRE_THROWS_AS(config.Finalize(), ConfigError);
    }
    SECTION("non-positive rate cap") {
        PipelineConfig config = BaseConfig();
        config.sector_rate_cap_per_hour = 0;
        REQUIRE_THROWS_AS(config.Finalize(), ConfigError);
    }
}

TEST_CASE("PipelineConfig loads the demo JSON subset", "[md][config]") {
    const PipelineConfig config =
        LoadPipelineConfigFromJson(ScenarioFile("example_md_config.json"));

    REQUIRE(*config.SectorOf("GOOGL.US") == "Web");
    REQUIRE(config.entry_threshold_pct == 3.0);
    REQUIRE(config.escalation_bands == std::vector<double>{3.0, 5.0, 8.0});
    REQUIRE(config.cooldown_minutes == 15);
    REQUIRE(config.sector_rate_cap_per_hour == 12);
    REQUIRE(config.critical_move_pct == 8.0);
    REQUIRE(config.simhash_hamming_threshold == 8);
}

TEST_CASE("PipelineConfig loader reports a missing file", "[md][config]") {
    REQUIRE_THROWS_AS(LoadPipelineConfigFromJson(ScenarioFile("no_such_config.json")),
                      ConfigError);
}

TEST_CASE("PipelineConfig loads the md.news poller block (Stage 8)", "[md][config]") {
    const PipelineConfig config =
        LoadPipelineConfigFromJson(ScenarioFile("example_md_config.json"));

    REQUIRE(config.news.poll_interval_seconds == 300);
    REQUIRE(config.news.min_call_spacing_ms == 250);
    REQUIRE(config.news.call_timeout_seconds == 20);
    REQUIRE(config.news.max_age_minutes == 120);
    REQUIRE(config.news.max_items_per_symbol == 5);
    REQUIRE(config.news.seen_capacity == 4096);
}

TEST_CASE("PipelineConfig rejects invalid news knobs", "[md][config]") {
    // Validated even when --news-source is none: a typo'd knob should be a
    // startup error, not a surprise the first time live news is switched on.
    SECTION("non-positive poll interval") {
        PipelineConfig config = BaseConfig();
        config.news.poll_interval_seconds = 0;
        REQUIRE_THROWS_AS(config.Finalize(), ConfigError);
    }
    SECTION("negative call spacing") {
        PipelineConfig config = BaseConfig();
        config.news.min_call_spacing_ms = -1;
        REQUIRE_THROWS_AS(config.Finalize(), ConfigError);
    }
    SECTION("negative age window") {
        PipelineConfig config = BaseConfig();
        config.news.max_age_minutes = -1;
        REQUIRE_THROWS_AS(config.Finalize(), ConfigError);
    }
    SECTION("zero items per symbol would silently disable news") {
        PipelineConfig config = BaseConfig();
        config.news.max_items_per_symbol = 0;
        REQUIRE_THROWS_AS(config.Finalize(), ConfigError);
    }
    SECTION("zero seen capacity") {
        PipelineConfig config = BaseConfig();
        config.news.seen_capacity = 0;
        REQUIRE_THROWS_AS(config.Finalize(), ConfigError);
    }
    SECTION("a valid news block passes") {
        PipelineConfig config = BaseConfig();
        REQUIRE_NOTHROW(config.Finalize());
    }
}
