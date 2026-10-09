// When multiple applicable rules fail, report the first failure in rule order.

#include <catch2/catch_test_macros.hpp>

#include "risk/engine.hpp"
#include "test_support.hpp"

using namespace tevnnis::risk;
using test::BaselineBuy;
using test::BaselineContext;
using test::BaselineHold;
using test::BaselineSell;

namespace {

void RequireRule(const RiskDecision& decision, const char* rule_id) {
    INFO("rule_id=" << decision.rule_id << " reason=" << decision.reason);
    REQUIRE(decision.rule_id == rule_id);
}

}  // namespace

TEST_CASE("HOLD outranks the kill switch", "[precedence]") {
    RiskContext context         = BaselineContext();
    context.kill_switch_engaged = true;
    RequireRule(evaluate(BaselineHold(), context), rules::kHoldNoOp);
}

TEST_CASE("the kill switch outranks a duplicate order", "[precedence]") {
    RiskContext context           = BaselineContext();
    context.kill_switch_engaged   = true;
    context.seen_client_order_ids = {"coid-buy-1"};
    RequireRule(evaluate(BaselineBuy(), context), rules::kKillSwitch);
}

TEST_CASE("a duplicate order outranks the universe allowlist", "[precedence]") {
    RiskContext        context     = BaselineContext();
    context.seen_client_order_ids  = {"coid-buy-1"};
    TradingInstruction instruction = BaselineBuy();
    instruction.symbol             = "TSLA.US";
    RequireRule(evaluate(instruction, context), rules::kDuplicateOrder);
}

TEST_CASE("a halt still blocks the exit of an out-of-universe holding", "[precedence]") {
    // The allowlist is skipped for a SELL, but a halt is a market reality and
    // stops the exit at rule 4 regardless.
    RiskContext context               = BaselineContext();
    context.positions                 = {{"TSLA.US", Position{10, 90.0}}};
    context.last_prices["TSLA.US"]    = 100.0;
    context.symbol_status["TSLA.US"]  = SymbolStatus::kHalted;

    TradingInstruction instruction = BaselineSell(10);
    instruction.symbol             = "TSLA.US";
    RequireRule(evaluate(instruction, context), rules::kSymbolHalted);
}

TEST_CASE("the universe allowlist outranks a halt for an entry", "[precedence]") {
    RiskContext        context      = BaselineContext();
    context.symbol_status["TSLA.US"] = SymbolStatus::kHalted;
    TradingInstruction instruction   = BaselineBuy();
    instruction.symbol               = "TSLA.US";
    RequireRule(evaluate(instruction, context), rules::kUniverseAllowlist);
}

TEST_CASE("a halt outranks the no-trade window", "[precedence]") {
    RiskContext context              = BaselineContext();
    context.symbol_status["NVDA.US"] = SymbolStatus::kHalted;
    context.session                  = MarketSession::kClosed;
    RequireRule(evaluate(BaselineBuy(), context), rules::kSymbolHalted);
}

TEST_CASE("the no-trade window outranks a missing last price", "[precedence]") {
    RiskContext context = BaselineContext();
    context.session     = MarketSession::kClosed;
    context.last_prices.erase("NVDA.US");
    RequireRule(evaluate(BaselineBuy(), context), rules::kNoTradeWindow);
}

TEST_CASE("a missing last price outranks the notional cap", "[precedence]") {
    RiskContext context = BaselineContext();
    context.last_prices.erase("NVDA.US");
    RequireRule(evaluate(BaselineBuy(1'000, 100.0), context), rules::kMissingLastPrice);
}

TEST_CASE("limit-price deviation outranks the notional cap", "[precedence]") {
    RiskContext context = BaselineContext();
    RequireRule(evaluate(BaselineBuy(1'000, 200.0), context), rules::kLimitPriceDeviation);
}

TEST_CASE("the notional cap outranks buying power", "[precedence]") {
    RiskContext context          = BaselineContext();
    context.account.buying_power = 0.0;
    RequireRule(evaluate(BaselineBuy(40, 100.0), context), rules::kMaxOrderNotional);
}

TEST_CASE("insufficient holdings outranks the PDT cap", "[precedence]") {
    RiskContext context                    = BaselineContext();
    context.positions                      = {{"NVDA.US", Position{1, 90.0}}};
    context.positions_opened_today         = {"NVDA.US"};
    context.day_trades_this_week           = 3;
    context.limits.max_day_trades_per_week = 3;
    RequireRule(evaluate(BaselineSell(10), context), rules::kInsufficientHoldings);
}

TEST_CASE("buying power outranks the cash reserve", "[precedence]") {
    RiskContext context          = BaselineContext();
    context.account.buying_power = 0.0;
    context.account.cash         = 0.0;
    RequireRule(evaluate(BaselineBuy(), context), rules::kBuyingPower);
}

TEST_CASE("the cash reserve outranks the position cap", "[precedence]") {
    RiskContext context  = BaselineContext();
    context.account.cash = 2'000.0;  // reserve 1000; a 2500 buy breaches both
    RequireRule(evaluate(BaselineBuy(25, 100.0), context), rules::kMinCashReserve);
}

TEST_CASE("the position cap outranks the sector cap", "[precedence]") {
    RiskContext context = BaselineContext();
    context.positions   = {{"AMD.US", Position{80, 45.0}}};  // sector already at 4000
    RequireRule(evaluate(BaselineBuy(26, 100.0), context), rules::kMaxPositionPct);
}

TEST_CASE("the sector cap outranks the daily trade count", "[precedence]") {
    RiskContext context                       = BaselineContext();
    context.positions                         = {{"AMD.US", Position{80, 45.0}}};
    context.budgets.broker_max_trades_per_day = 1;
    context.trades_today                      = 1;
    RequireRule(evaluate(BaselineBuy(25, 100.0), context), rules::kMaxSectorPct);
}

TEST_CASE("the daily trade count outranks daily turnover", "[precedence]") {
    RiskContext context                         = BaselineContext();
    context.budgets.broker_max_trades_per_day   = 1;
    context.trades_today                        = 1;
    context.budgets.broker_max_turnover_per_day = 100.0;
    RequireRule(evaluate(BaselineBuy(), context), rules::kDailyTradeCount);
}
