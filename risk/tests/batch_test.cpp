// Verify that batch evaluation checks cumulative projected effects,
// rather than evaluating every instruction against the starting state.

#include <catch2/catch_test_macros.hpp>

#include "risk/engine.hpp"
#include "test_support.hpp"

using namespace tevnnis::risk;
using test::BaselineBuy;
using test::BaselineContext;
using test::BaselineHold;
using test::BaselineSell;

namespace {

TradingInstruction Buy(const std::string& symbol, std::int64_t quantity, double limit_price,
                       const std::string& client_order_id) {
    TradingInstruction instruction = BaselineBuy(quantity, limit_price, client_order_id);
    instruction.symbol             = symbol;
    return instruction;
}

TradingInstruction Sell(const std::string& symbol, std::int64_t quantity, double limit_price,
                        const std::string& client_order_id) {
    TradingInstruction instruction = BaselineSell(quantity, limit_price, client_order_id);
    instruction.symbol             = symbol;
    return instruction;
}

}  // namespace

TEST_CASE("a batch cannot jointly breach the cash reserve", "[batch]") {
    RiskContext context   = BaselineContext();
    context.account.cash  = 3'000.0;  // reserve is 1000

    const TradingInstruction first  = Buy("NVDA.US", 10, 100.0, "coid-1");   // 1000
    const TradingInstruction second = Buy("NVDA.US", 15, 100.0, "coid-2");   // 1500

    // Each is fine on its own against the starting context.
    REQUIRE(evaluate(first, context).allowed());
    REQUIRE(evaluate(second, context).allowed());

    // Together they are not: 3000 - 1000 - 1500 = 500 < the 1000 reserve.
    const auto decisions = EvaluateBatch({first, second}, context);
    REQUIRE(decisions.size() == 2);
    REQUIRE(decisions[0].allowed());
    REQUIRE_FALSE(decisions[1].allowed());
    REQUIRE(decisions[1].rule_id == rules::kMinCashReserve);
}

TEST_CASE("a batch cannot jointly breach the sector cap", "[batch]") {
    RiskContext context               = BaselineContext();
    context.limits.max_position_pct   = 0.30;  // 3000, so the sector cap binds first
    context.positions                 = {{"AMD.US", Position{30, 50.0}}};  // 1500

    const TradingInstruction first  = Buy("NVDA.US", 25, 100.0, "coid-1");  // 2500
    const TradingInstruction second = Buy("AMD.US", 25, 50.0, "coid-2");    // 1250

    REQUIRE(evaluate(first, context).allowed());
    REQUIRE(evaluate(second, context).allowed());

    // Semiconductor cap is 5000; jointly the sector would reach 5250.
    const auto decisions = EvaluateBatch({first, second}, context);
    REQUIRE(decisions[0].allowed());
    REQUIRE_FALSE(decisions[1].allowed());
    REQUIRE(decisions[1].rule_id == rules::kMaxSectorPct);
}

TEST_CASE("a client_order_id reused within one batch is caught", "[batch]") {
    const RiskContext context = BaselineContext();

    const auto decisions = EvaluateBatch(
        {Buy("NVDA.US", 5, 100.0, "coid-dup"), Buy("AMD.US", 5, 50.0, "coid-dup")}, context);

    REQUIRE(decisions[0].allowed());
    REQUIRE_FALSE(decisions[1].allowed());
    REQUIRE(decisions[1].rule_id == rules::kDuplicateOrder);
}

TEST_CASE("the daily trade count is consumed across a batch", "[batch]") {
    RiskContext context                       = BaselineContext();
    context.budgets.broker_max_trades_per_day = 2;

    const auto decisions = EvaluateBatch({Buy("NVDA.US", 1, 100.0, "coid-1"),
                                          Buy("AMD.US", 1, 50.0, "coid-2"),
                                          Buy("XOM.US", 1, 100.0, "coid-3")},
                                         context);

    REQUIRE(decisions[0].allowed());
    REQUIRE(decisions[1].allowed());
    REQUIRE_FALSE(decisions[2].allowed());
    REQUIRE(decisions[2].rule_id == rules::kDailyTradeCount);
}

TEST_CASE("daily turnover accumulates across a batch", "[batch]") {
    RiskContext context                         = BaselineContext();
    context.budgets.broker_max_turnover_per_day = 2'500.0;

    const auto decisions = EvaluateBatch({Buy("NVDA.US", 10, 100.0, "coid-1"),
                                          Buy("AMD.US", 20, 50.0, "coid-2"),
                                          Buy("XOM.US", 10, 100.0, "coid-3")},
                                         context);

    REQUIRE(decisions[0].allowed());   // 1000
    REQUIRE(decisions[1].allowed());   // 2000
    REQUIRE_FALSE(decisions[2].allowed());  // 3000 > 2500
    REQUIRE(decisions[2].rule_id == rules::kDailyTurnover);
}

TEST_CASE("buying then selling within one batch consumes a PDT day trade", "[batch][pdt]") {
    RiskContext context                    = BaselineContext();
    context.day_trades_this_week           = 2;
    context.limits.max_day_trades_per_week = 3;

    const auto decisions = EvaluateBatch({Buy("NVDA.US", 10, 100.0, "coid-1"),
                                          Sell("NVDA.US", 10, 100.0, "coid-2"),
                                          Buy("AMD.US", 10, 50.0, "coid-3"),
                                          Sell("AMD.US", 10, 50.0, "coid-4")},
                                         context);

    REQUIRE(decisions[0].allowed());
    REQUIRE(decisions[1].allowed());        // day trade 3 of 3
    REQUIRE(decisions[2].allowed());
    REQUIRE_FALSE(decisions[3].allowed());  // would be day trade 4
    REQUIRE(decisions[3].rule_id == rules::kPdtDayTradeCap);
}

TEST_CASE("a sell earlier in the batch frees cash for a later buy", "[batch]") {
    RiskContext context  = BaselineContext();
    context.account.cash = 1'500.0;  // reserve 1000
    context.positions    = {{"XOM.US", Position{10, 95.0}}};

    const TradingInstruction buy = Buy("NVDA.US", 10, 100.0, "coid-2");

    // On its own the buy would leave only 500 against a 1000 reserve.
    REQUIRE(evaluate(buy, context).rule_id == rules::kMinCashReserve);

    const auto decisions = EvaluateBatch({Sell("XOM.US", 10, 100.0, "coid-1"), buy}, context);
    REQUIRE(decisions[0].allowed());
    REQUIRE(decisions[1].allowed());  // sale proceeds lifted cash to 2500
}

TEST_CASE("a rejected instruction consumes no provisional budget", "[batch]") {
    RiskContext context                       = BaselineContext();
    context.budgets.broker_max_trades_per_day = 1;

    const auto decisions = EvaluateBatch({Buy("NVDA.US", 26, 100.0, "coid-1"),  // over position cap
                                          Buy("NVDA.US", 10, 100.0, "coid-2")},
                                         context);

    REQUIRE_FALSE(decisions[0].allowed());
    REQUIRE(decisions[0].rule_id == rules::kMaxPositionPct);
    REQUIRE(decisions[1].allowed());  // the single trade slot is still free
}

TEST_CASE("a HOLD in a batch consumes nothing", "[batch]") {
    RiskContext context                       = BaselineContext();
    context.budgets.broker_max_trades_per_day = 1;

    const auto decisions =
        EvaluateBatch({BaselineHold(), Buy("NVDA.US", 10, 100.0, "coid-1")}, context);

    REQUIRE(decisions[0].allowed());
    REQUIRE(decisions[0].rule_id == rules::kHoldNoOp);
    REQUIRE(decisions[1].allowed());
}

TEST_CASE("EvaluateBatch does not mutate the caller's context", "[batch][purity]") {
    RiskContext context = BaselineContext();

    const double cash_before   = context.account.cash;
    const int    trades_before = context.trades_today;
    const auto   seen_before   = context.seen_client_order_ids;

    const auto decisions =
        EvaluateBatch({Buy("NVDA.US", 10, 100.0, "coid-1"), Buy("AMD.US", 10, 50.0, "coid-2")},
                      context);
    REQUIRE(decisions[0].allowed());
    REQUIRE(decisions[1].allowed());

    REQUIRE(context.account.cash == cash_before);
    REQUIRE(context.trades_today == trades_before);
    REQUIRE(context.seen_client_order_ids == seen_before);
    REQUIRE(context.positions.empty());
}

TEST_CASE("an empty batch yields no decisions", "[batch]") {
    REQUIRE(EvaluateBatch({}, BaselineContext()).empty());
}

// ApplyAllowed: the provisional-state transition itself

TEST_CASE("ApplyAllowed applies a buy's projected effects", "[apply]") {
    RiskContext context = BaselineContext();
    ApplyAllowed(Buy("NVDA.US", 10, 100.0, "coid-1"), context);

    REQUIRE(context.account.cash == 9'000.0);
    REQUIRE(context.account.buying_power == 9'000.0);
    REQUIRE(context.HeldQuantity("NVDA.US") == 10);
    REQUIRE(context.positions.at("NVDA.US").avg_cost == 100.0);
    REQUIRE(context.trades_today == 1);
    REQUIRE(context.turnover_today == 1'000.0);
    REQUIRE(context.seen_client_order_ids.count("coid-1") == 1);
    REQUIRE(context.positions_opened_today.count("NVDA.US") == 1);
    REQUIRE(context.day_trades_this_week == 0);
}

TEST_CASE("ApplyAllowed blends the average cost on a follow-up buy", "[apply]") {
    RiskContext context = BaselineContext();
    context.positions   = {{"NVDA.US", Position{10, 90.0}}};

    ApplyAllowed(Buy("NVDA.US", 10, 100.0, "coid-1"), context);
    REQUIRE(context.HeldQuantity("NVDA.US") == 20);
    REQUIRE(context.positions.at("NVDA.US").avg_cost == 95.0);
}

TEST_CASE("ApplyAllowed applies a sell's projected effects", "[apply]") {
    RiskContext context = BaselineContext();
    context.positions   = {{"NVDA.US", Position{25, 90.0}}};

    ApplyAllowed(Sell("NVDA.US", 10, 100.0, "coid-1"), context);
    REQUIRE(context.account.cash == 11'000.0);
    REQUIRE(context.HeldQuantity("NVDA.US") == 15);
    REQUIRE(context.trades_today == 1);
    REQUIRE(context.turnover_today == 1'000.0);
    REQUIRE(context.day_trades_this_week == 0);  // not opened today
}

TEST_CASE("ApplyAllowed drops a fully closed position", "[apply]") {
    RiskContext context = BaselineContext();
    context.positions   = {{"NVDA.US", Position{10, 90.0}}};

    ApplyAllowed(Sell("NVDA.US", 10, 100.0, "coid-1"), context);
    REQUIRE(context.positions.count("NVDA.US") == 0);
    REQUIRE(context.HeldQuantity("NVDA.US") == 0);
}

TEST_CASE("ApplyAllowed counts a same-day close as a day trade", "[apply]") {
    RiskContext context            = BaselineContext();
    context.positions              = {{"NVDA.US", Position{10, 100.0}}};
    context.positions_opened_today = {"NVDA.US"};

    ApplyAllowed(Sell("NVDA.US", 10, 100.0, "coid-1"), context);
    REQUIRE(context.day_trades_this_week == 1);
}

TEST_CASE("ApplyAllowed ignores a HOLD", "[apply]") {
    RiskContext context = BaselineContext();
    ApplyAllowed(BaselineHold(), context);

    REQUIRE(context.account.cash == 10'000.0);
    REQUIRE(context.trades_today == 0);
    REQUIRE(context.turnover_today == 0.0);
    REQUIRE(context.seen_client_order_ids.empty());
    REQUIRE(context.positions.empty());
}
