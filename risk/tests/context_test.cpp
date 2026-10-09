// RiskContext::Finalize(): the universe index and context validation.
// A malformed context is a config error, never a silent reject.

#include <catch2/catch_test_macros.hpp>

#include <limits>

#include "risk/context.hpp"
#include "test_support.hpp"

using namespace tevnnis::risk;
using test::BaselineContext;

TEST_CASE("Finalize builds the symbol to sector index", "[context]") {
    const RiskContext context = BaselineContext();

    REQUIRE(context.finalized());
    REQUIRE(context.symbol_sector().size() == 5);
    REQUIRE(*context.SectorOf("NVDA.US") == "Semiconductor");
    REQUIRE(*context.SectorOf("AMD.US") == "Semiconductor");
    REQUIRE(*context.SectorOf("XOM.US") == "Energy");
    REQUIRE(*context.SectorOf("META.US") == "Web");
    REQUIRE(context.SectorOf("TSLA.US") == nullptr);
}

TEST_CASE("the universe is the allowlist", "[context]") {
    const RiskContext context = BaselineContext();
    REQUIRE(context.InUniverse("NVDA.US"));
    REQUIRE_FALSE(context.InUniverse("TSLA.US"));
    REQUIRE_FALSE(context.InUniverse(""));
    REQUIRE_FALSE(context.InUniverse("nvda.us"));  // symbols are case-sensitive
}

TEST_CASE("SymbolsInSector returns the configured members", "[context]") {
    const RiskContext context = BaselineContext();
    REQUIRE(context.SymbolsInSector("Semiconductor").size() == 2);
    REQUIRE(context.SymbolsInSector("Nonexistent").empty());
}

TEST_CASE("a fresh context is not finalized", "[context]") {
    const RiskContext context;
    REQUIRE_FALSE(context.finalized());
}

TEST_CASE("an empty universe is rejected", "[context]") {
    RiskContext context;
    context.account.managed_capital = 10'000.0;
    REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
}

TEST_CASE("an empty sector is rejected", "[context]") {
    RiskContext context             = BaselineContext();
    context.universe["Empty"]       = {};
    REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
}

TEST_CASE("an empty symbol is rejected", "[context]") {
    RiskContext context      = BaselineContext();
    context.universe["Energy"] = {"XOM.US", ""};
    REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
}

TEST_CASE("a symbol in two sectors is rejected", "[context]") {
    // Otherwise the sector cap would be ambiguous for that symbol.
    RiskContext context      = BaselineContext();
    context.universe["Energy"] = {"XOM.US", "NVDA.US"};
    REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
}

TEST_CASE("a symbol repeated within one sector is fine", "[context]") {
    RiskContext context             = BaselineContext();
    context.universe["Energy"]      = {"XOM.US", "XOM.US"};
    REQUIRE_NOTHROW(context.Finalize());
    REQUIRE(*context.SectorOf("XOM.US") == "Energy");
}

TEST_CASE("out-of-range percentage limits are rejected", "[context]") {
    SECTION("max_position_pct above 1") {
        RiskContext context             = BaselineContext();
        context.limits.max_position_pct = 1.5;
        REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
    }
    SECTION("negative max_sector_pct") {
        RiskContext context           = BaselineContext();
        context.limits.max_sector_pct = -0.1;
        REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
    }
    SECTION("negative min_cash_reserve_pct") {
        RiskContext context                 = BaselineContext();
        context.limits.min_cash_reserve_pct = -0.01;
        REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
    }
    SECTION("deviation above 1") {
        RiskContext context                          = BaselineContext();
        context.limits.limit_price_max_deviation_pct = 2.0;
        REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
    }
}

TEST_CASE("a non-positive max_order_notional is rejected", "[context]") {
    RiskContext context               = BaselineContext();
    context.limits.max_order_notional = 0.0;
    REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
}

TEST_CASE("negative caps and windows are rejected", "[context]") {
    SECTION("max_day_trades_per_week") {
        RiskContext context                    = BaselineContext();
        context.limits.max_day_trades_per_week = -1;
        REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
    }
    SECTION("no-trade window minutes") {
        RiskContext context                        = BaselineContext();
        context.limits.no_trade_before_close_minutes = -5;
        REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
    }
    SECTION("broker_max_trades_per_day") {
        RiskContext context                       = BaselineContext();
        context.budgets.broker_max_trades_per_day = -1;
        REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
    }
    SECTION("broker_max_turnover_per_day") {
        RiskContext context                         = BaselineContext();
        context.budgets.broker_max_turnover_per_day = -1.0;
        REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
    }
}

TEST_CASE("managed capital must be positive", "[context]") {
    // It is the denominator for every percentage cap.
    RiskContext context             = BaselineContext();
    context.account.managed_capital = 0.0;
    REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
}

TEST_CASE("a non-finite account value is rejected", "[context]") {
    RiskContext context  = BaselineContext();
    context.account.cash = std::numeric_limits<double>::quiet_NaN();
    REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
}

TEST_CASE("a session that closes before it opens is rejected", "[context]") {
    RiskContext context     = BaselineContext();
    context.market_close_ts = context.market_open_ts - 1;
    REQUIRE_THROWS_AS(context.Finalize(), RiskConfigError);
}
