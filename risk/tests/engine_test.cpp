// rule coverage: every check, allow path and reject path.
//
// Each test starts from the permissive baseline and breaks exactly one input.

#include <catch2/catch_test_macros.hpp>

#include <cmath>
#include <limits>

#include "risk/engine.hpp"
#include "test_support.hpp"

using namespace tevnnis::risk;
using test::BaselineBuy;
using test::BaselineContext;
using test::BaselineHold;
using test::BaselineSell;

namespace {

void RequireRejected(const RiskDecision& decision, const char* rule_id) {
    INFO("rule_id=" << decision.rule_id << " reason=" << decision.reason);
    REQUIRE_FALSE(decision.allowed());
    REQUIRE(decision.verdict == Verdict::kReject);
    REQUIRE(decision.rule_id == rule_id);
    REQUIRE_FALSE(decision.reason.empty());  // the audit row must say why
}

void RequireAllowed(const RiskDecision& decision, const char* rule_id = rules::kAllowed) {
    INFO("rule_id=" << decision.rule_id << " reason=" << decision.reason);
    REQUIRE(decision.allowed());
    REQUIRE(decision.verdict == Verdict::kAllow);
    REQUIRE(decision.rule_id == rule_id);
}

}  // namespace

// HOLD_NO_OP

TEST_CASE("HOLD is allowed as a no-op", "[rule][hold]") {
    RequireAllowed(evaluate(BaselineHold(), BaselineContext()), rules::kHoldNoOp);
}

TEST_CASE("HOLD short-circuits ahead of every other check", "[rule][hold]") {
    // A HOLD places no order, so "rejected: kill switch" would be a nonsense
    // audit row. HOLD is logged and permitted unconditionally.
    RiskContext context      = BaselineContext();
    context.kill_switch_engaged = true;
    context.session             = MarketSession::kClosed;

    TradingInstruction hold = BaselineHold();
    hold.symbol             = "NOTREAL.US";  // outside the universe
    hold.client_order_id.clear();

    RequireAllowed(evaluate(hold, context), rules::kHoldNoOp);
}

// KILL_SWITCH

TEST_CASE("kill switch blocks all trading", "[rule][kill_switch]") {
    RiskContext context         = BaselineContext();
    context.kill_switch_engaged = true;

    RequireRejected(evaluate(BaselineBuy(), context), rules::kKillSwitch);
    RequireRejected(evaluate(BaselineSell(), context), rules::kKillSwitch);
}

TEST_CASE("kill switch disengaged allows trading", "[rule][kill_switch]") {
    RiskContext context         = BaselineContext();
    context.kill_switch_engaged = false;
    RequireAllowed(evaluate(BaselineBuy(), context));
}

// DUPLICATE_ORDER (idempotency)

TEST_CASE("a client_order_id already seen is rejected", "[rule][duplicate]") {
    RiskContext context = BaselineContext();
    context.seen_client_order_ids = {"coid-buy-1"};
    RequireRejected(evaluate(BaselineBuy(), context), rules::kDuplicateOrder);
}

TEST_CASE("an empty client_order_id is rejected", "[rule][duplicate]") {
    RiskContext        context     = BaselineContext();
    TradingInstruction instruction = BaselineBuy();
    instruction.client_order_id.clear();
    RequireRejected(evaluate(instruction, context), rules::kDuplicateOrder);
}

TEST_CASE("an unseen client_order_id is allowed", "[rule][duplicate]") {
    RiskContext context           = BaselineContext();
    context.seen_client_order_ids = {"coid-something-else"};
    RequireAllowed(evaluate(BaselineBuy(), context));
}

// UNIVERSE_ALLOWLIST

TEST_CASE("buying a symbol outside the configured universe is rejected", "[rule][universe]") {
    RiskContext        context     = BaselineContext();
    TradingInstruction instruction = BaselineBuy();
    instruction.symbol             = "TSLA.US";  // plausible, but not configured
    context.last_prices["TSLA.US"] = 100.0;      // priced, still not allowed
    RequireRejected(evaluate(instruction, context), rules::kUniverseAllowlist);
}

TEST_CASE("a held symbol outside the universe can still be sold", "[rule][universe]") {
    // The allowlist governs entries. A position dropped from the universe must
    // remain closeable, or a config change would trap it.
    RiskContext        context     = BaselineContext();
    context.positions              = {{"TSLA.US", Position{10, 90.0}}};
    context.last_prices["TSLA.US"] = 100.0;

    TradingInstruction instruction = BaselineSell(10);
    instruction.symbol             = "TSLA.US";
    REQUIRE(context.SectorOf("TSLA.US") == nullptr);  // no sector to dereference
    RequireAllowed(evaluate(instruction, context));
}

TEST_CASE("selling an out-of-universe symbol is still bounded by holdings",
          "[rule][universe]") {
    RiskContext        context     = BaselineContext();
    context.positions              = {{"TSLA.US", Position{5, 90.0}}};
    context.last_prices["TSLA.US"] = 100.0;

    TradingInstruction instruction = BaselineSell(10);
    instruction.symbol             = "TSLA.US";
    RequireRejected(evaluate(instruction, context), rules::kInsufficientHoldings);
}

TEST_CASE("every configured symbol passes the allowlist", "[rule][universe]") {
    RiskContext context = BaselineContext();
    for (const auto& symbol : {"NVDA.US", "AMD.US", "XOM.US", "GOOGL.US", "META.US"}) {
        TradingInstruction instruction = BaselineBuy(1, *context.LastPrice(symbol));
        instruction.symbol             = symbol;
        RequireAllowed(evaluate(instruction, context));
    }
}

// SYMBOL_HALTED (no-trade window)

TEST_CASE("a halted symbol cannot be traded", "[rule][halt]") {
    RiskContext context                = BaselineContext();
    context.symbol_status["NVDA.US"]   = SymbolStatus::kHalted;
    RequireRejected(evaluate(BaselineBuy(), context), rules::kSymbolHalted);
}

TEST_CASE("a halt on another symbol does not block this one", "[rule][halt]") {
    RiskContext context              = BaselineContext();
    context.symbol_status["AMD.US"]  = SymbolStatus::kHalted;
    context.symbol_status["NVDA.US"] = SymbolStatus::kNormal;
    RequireAllowed(evaluate(BaselineBuy(), context));
}

TEST_CASE("an unreported status is treated as normal", "[rule][halt]") {
    RiskContext context = BaselineContext();
    REQUIRE(context.symbol_status.empty());
    REQUIRE(context.StatusOf("NVDA.US") == SymbolStatus::kNormal);
    RequireAllowed(evaluate(BaselineBuy(), context));
}

// NO_TRADE_WINDOW (session + open/close volatility windows)

TEST_CASE("trading outside an open session is rejected", "[rule][window]") {
    for (const auto session : {MarketSession::kClosed, MarketSession::kPreMarket,
                               MarketSession::kPostMarket}) {
        RiskContext context = BaselineContext();
        context.session     = session;
        RequireRejected(evaluate(BaselineBuy(), context), rules::kNoTradeWindow);
    }
}

TEST_CASE("the opening volatility window is closed to trading", "[rule][window]") {
    RiskContext context = BaselineContext();
    context.now_epoch_s = test::kMarketOpenTs + 120;  // 2 min in, window is 5
    RequireRejected(evaluate(BaselineBuy(), context), rules::kNoTradeWindow);

    // The boundary itself is tradable.
    context.now_epoch_s = test::kMarketOpenTs + 300;
    RequireAllowed(evaluate(BaselineBuy(), context));
}

TEST_CASE("the closing volatility window is closed to trading", "[rule][window]") {
    RiskContext context = BaselineContext();
    context.now_epoch_s = test::kMarketCloseTs - 120;  // 2 min out, window is 5
    RequireRejected(evaluate(BaselineBuy(), context), rules::kNoTradeWindow);

    context.now_epoch_s = test::kMarketCloseTs - 301;
    RequireAllowed(evaluate(BaselineBuy(), context));
}

TEST_CASE("window checks are skipped when session bounds are unset", "[rule][window]") {
    RiskContext context     = BaselineContext();
    context.market_open_ts  = 0;
    context.market_close_ts = 0;
    context.now_epoch_s     = 0;
    RequireAllowed(evaluate(BaselineBuy(), context));
}

TEST_CASE("a zero-minute window disables itself", "[rule][window]") {
    RiskContext context                        = BaselineContext();
    context.limits.no_trade_after_open_minutes = 0;
    context.now_epoch_s                        = test::kMarketOpenTs;
    RequireAllowed(evaluate(BaselineBuy(), context));
}

// MISSING_LAST_PRICE

TEST_CASE("an unpriced symbol cannot be bought", "[rule][last_price]") {
    RiskContext context = BaselineContext();
    context.last_prices.erase("NVDA.US");
    RequireRejected(evaluate(BaselineBuy(), context), rules::kMissingLastPrice);
}

TEST_CASE("a non-positive or non-finite last price blocks a buy", "[rule][last_price]") {
    for (const double bad : {0.0, -10.0, std::numeric_limits<double>::quiet_NaN(),
                             std::numeric_limits<double>::infinity()}) {
        RiskContext context            = BaselineContext();
        context.last_prices["NVDA.US"] = bad;
        RequireRejected(evaluate(BaselineBuy(), context), rules::kMissingLastPrice);
    }
}

TEST_CASE("an unpriced symbol can still be sold", "[rule][last_price]") {
    // An exit must not be blocked for lack of a fresh quote.
    RiskContext context = BaselineContext();
    context.positions   = {{"NVDA.US", Position{10, 90.0}}};
    context.last_prices.erase("NVDA.US");
    RequireAllowed(evaluate(BaselineSell(10), context));
}

TEST_CASE("a non-positive or non-finite last price does not block a sell",
          "[rule][last_price]") {
    for (const double bad : {0.0, -10.0, std::numeric_limits<double>::quiet_NaN(),
                             std::numeric_limits<double>::infinity()}) {
        RiskContext context            = BaselineContext();
        context.positions              = {{"NVDA.US", Position{10, 90.0}}};
        context.last_prices["NVDA.US"] = bad;
        RequireAllowed(evaluate(BaselineSell(10), context));
    }
}

TEST_CASE("a priced symbol passes", "[rule][last_price]") {
    RiskContext context            = BaselineContext();
    context.last_prices["NVDA.US"] = 100.0;
    RequireAllowed(evaluate(BaselineBuy(), context));
}

// LIMIT_PRICE_DEVIATION

TEST_CASE("a limit price too far above last is rejected", "[rule][deviation]") {
    RiskContext context = BaselineContext();  // last 100, max deviation 3%
    RequireRejected(evaluate(BaselineBuy(10, 105.0), context), rules::kLimitPriceDeviation);
}

TEST_CASE("a limit price too far below last is rejected", "[rule][deviation]") {
    RiskContext context = BaselineContext();
    RequireRejected(evaluate(BaselineBuy(10, 95.0), context), rules::kLimitPriceDeviation);
}

TEST_CASE("a sell with a usable price is still deviation-checked", "[rule][deviation]") {
    // Only the ABSENCE of a price is tolerated on an exit, never a bad price.
    RiskContext context = BaselineContext();
    context.positions   = {{"NVDA.US", Position{10, 90.0}}};
    RequireRejected(evaluate(BaselineSell(10, 105.0), context), rules::kLimitPriceDeviation);
    RequireRejected(evaluate(BaselineSell(10, 95.0), context), rules::kLimitPriceDeviation);
}

TEST_CASE("deviation exactly at the limit is allowed", "[rule][deviation]") {
    RiskContext context = BaselineContext();
    RequireAllowed(evaluate(BaselineBuy(10, 103.0), context));
    RequireAllowed(evaluate(BaselineBuy(10, 97.0), context));
}

// MAX_ORDER_NOTIONAL

TEST_CASE("an order above max_order_notional is rejected", "[rule][notional]") {
    RiskContext context                = BaselineContext();
    context.limits.max_order_notional  = 1'000.0;
    RequireRejected(evaluate(BaselineBuy(11, 100.0), context), rules::kMaxOrderNotional);
}

TEST_CASE("an order exactly at max_order_notional is allowed", "[rule][notional]") {
    RiskContext context               = BaselineContext();
    context.limits.max_order_notional = 1'000.0;
    RequireAllowed(evaluate(BaselineBuy(10, 100.0), context));
}

TEST_CASE("max_order_notional applies to sells too", "[rule][notional]") {
    RiskContext context               = BaselineContext();
    context.limits.max_order_notional = 1'000.0;
    context.positions                 = {{"NVDA.US", Position{100, 90.0}}};
    RequireRejected(evaluate(BaselineSell(11, 100.0), context), rules::kMaxOrderNotional);
}

// INSUFFICIENT_HOLDINGS (no shorting in v0)

TEST_CASE("selling more than is held is rejected", "[rule][holdings]") {
    RiskContext context = BaselineContext();
    context.positions   = {{"NVDA.US", Position{5, 90.0}}};
    RequireRejected(evaluate(BaselineSell(10), context), rules::kInsufficientHoldings);
}

TEST_CASE("selling with no position at all is rejected", "[rule][holdings]") {
    RiskContext context = BaselineContext();
    REQUIRE(context.positions.empty());
    RequireRejected(evaluate(BaselineSell(10), context), rules::kInsufficientHoldings);
}

TEST_CASE("selling exactly the held quantity is allowed", "[rule][holdings]") {
    RiskContext context = BaselineContext();
    context.positions   = {{"NVDA.US", Position{10, 90.0}}};
    RequireAllowed(evaluate(BaselineSell(10), context));
}

// PDT_DAY_TRADE_CAP

TEST_CASE("a day trade beyond the weekly cap is rejected", "[rule][pdt]") {
    RiskContext context                   = BaselineContext();
    context.positions                     = {{"NVDA.US", Position{10, 100.0}}};
    context.positions_opened_today        = {"NVDA.US"};
    context.day_trades_this_week          = 3;
    context.limits.max_day_trades_per_week = 3;

    REQUIRE(IsDayTrade(BaselineSell(10), context));
    RequireRejected(evaluate(BaselineSell(10), context), rules::kPdtDayTradeCap);
}

TEST_CASE("a day trade within the weekly cap is allowed", "[rule][pdt]") {
    RiskContext context                    = BaselineContext();
    context.positions                      = {{"NVDA.US", Position{10, 100.0}}};
    context.positions_opened_today         = {"NVDA.US"};
    context.day_trades_this_week           = 2;
    context.limits.max_day_trades_per_week = 3;
    RequireAllowed(evaluate(BaselineSell(10), context));
}

TEST_CASE("selling a position not opened today is not a day trade", "[rule][pdt]") {
    RiskContext context                    = BaselineContext();
    context.positions                      = {{"NVDA.US", Position{10, 90.0}}};
    context.positions_opened_today         = {"AMD.US"};
    context.day_trades_this_week           = 3;
    context.limits.max_day_trades_per_week = 3;

    REQUIRE_FALSE(IsDayTrade(BaselineSell(10), context));
    RequireAllowed(evaluate(BaselineSell(10), context));
}

TEST_CASE("a buy is never a day trade", "[rule][pdt]") {
    RiskContext context                    = BaselineContext();
    context.positions_opened_today         = {"NVDA.US"};
    context.day_trades_this_week           = 3;
    context.limits.max_day_trades_per_week = 3;

    REQUIRE_FALSE(IsDayTrade(BaselineBuy(), context));
    RequireAllowed(evaluate(BaselineBuy(), context));
}

// BUYING_POWER

TEST_CASE("a buy beyond buying power is rejected", "[rule][buying_power]") {
    RiskContext context          = BaselineContext();
    context.account.buying_power = 500.0;  // order is 1000
    RequireRejected(evaluate(BaselineBuy(), context), rules::kBuyingPower);
}

TEST_CASE("a buy exactly at buying power is allowed", "[rule][buying_power]") {
    RiskContext context          = BaselineContext();
    context.account.buying_power = 1'000.0;
    RequireAllowed(evaluate(BaselineBuy(), context));
}

TEST_CASE("buying power does not gate a sell", "[rule][buying_power]") {
    RiskContext context          = BaselineContext();
    context.account.buying_power = 0.0;
    context.positions            = {{"NVDA.US", Position{10, 90.0}}};
    RequireAllowed(evaluate(BaselineSell(10), context));
}

// MIN_CASH_RESERVE

TEST_CASE("a buy that breaches the cash reserve is rejected", "[rule][cash_reserve]") {
    RiskContext context   = BaselineContext();  // reserve = 10% of 10000 = 1000
    context.account.cash  = 1'500.0;            // 1500 - 1000 = 500 < 1000
    RequireRejected(evaluate(BaselineBuy(), context), rules::kMinCashReserve);
}

TEST_CASE("a buy landing exactly on the cash reserve is allowed", "[rule][cash_reserve]") {
    RiskContext context  = BaselineContext();
    context.account.cash = 2'000.0;  // 2000 - 1000 = 1000 == reserve
    RequireAllowed(evaluate(BaselineBuy(), context));
}

TEST_CASE("a zero cash reserve permits spending down to zero", "[rule][cash_reserve]") {
    RiskContext context                 = BaselineContext();
    context.limits.min_cash_reserve_pct = 0.0;
    context.account.cash                = 1'000.0;
    RequireAllowed(evaluate(BaselineBuy(), context));
}

// MAX_POSITION_PCT

TEST_CASE("a buy beyond the per-symbol cap is rejected", "[rule][position_cap]") {
    RiskContext context = BaselineContext();  // cap = 25% of 10000 = 2500
    RequireRejected(evaluate(BaselineBuy(26, 100.0), context), rules::kMaxPositionPct);
}

TEST_CASE("a buy exactly at the per-symbol cap is allowed", "[rule][position_cap]") {
    RiskContext context = BaselineContext();
    RequireAllowed(evaluate(BaselineBuy(25, 100.0), context));
}

TEST_CASE("the existing holding counts toward the per-symbol cap", "[rule][position_cap]") {
    RiskContext context = BaselineContext();
    context.positions   = {{"NVDA.US", Position{20, 90.0}}};  // 2000 at last price
    RequireRejected(evaluate(BaselineBuy(10, 100.0), context), rules::kMaxPositionPct);
    RequireAllowed(evaluate(BaselineBuy(5, 100.0), context));  // 25 * 100 = 2500
}

TEST_CASE("the position cap values the order at the worse of last and limit",
          "[rule][position_cap]") {
    // 25 shares is exactly at the cap when valued at last (100), but the order
    // may fill as high as its limit (103), so the conservative valuation binds.
    RiskContext context = BaselineContext();
    RequireRejected(evaluate(BaselineBuy(25, 103.0), context), rules::kMaxPositionPct);
    RequireAllowed(evaluate(BaselineBuy(24, 103.0), context));  // 24 * 103 = 2472
}

TEST_CASE("the position cap does not gate a sell", "[rule][position_cap]") {
    RiskContext context = BaselineContext();
    context.positions   = {{"NVDA.US", Position{100, 90.0}}};  // already far over cap
    RequireAllowed(evaluate(BaselineSell(10), context));
}

// MAX_SECTOR_PCT

TEST_CASE("a buy beyond the sector cap is rejected", "[rule][sector_cap]") {
    // Sector cap = 50% of 10000 = 5000. AMD holding is 80 * 50 = 4000.
    RiskContext context = BaselineContext();
    context.positions   = {{"AMD.US", Position{80, 45.0}}};
    RequireRejected(evaluate(BaselineBuy(25, 100.0), context), rules::kMaxSectorPct);
}

TEST_CASE("a buy within the sector cap is allowed", "[rule][sector_cap]") {
    RiskContext context = BaselineContext();
    context.positions   = {{"AMD.US", Position{20, 45.0}}};  // 1000
    RequireAllowed(evaluate(BaselineBuy(25, 100.0), context));  // 1000 + 2500 = 3500
}

TEST_CASE("holdings in other sectors do not count", "[rule][sector_cap]") {
    RiskContext context = BaselineContext();
    context.positions   = {{"META.US", Position{20, 400.0}}};  // 8000, Web sector
    RequireAllowed(evaluate(BaselineBuy(25, 100.0), context));
}

TEST_CASE("an unpriced holding is valued at its cost basis", "[rule][sector_cap]") {
    RiskContext context = BaselineContext();
    context.positions   = {{"AMD.US", Position{80, 50.0}}};
    context.last_prices.erase("AMD.US");  // never priced by md
    // Falls back to 80 * 50 = 4000 rather than valuing the holding at zero.
    RequireRejected(evaluate(BaselineBuy(25, 100.0), context), rules::kMaxSectorPct);
}

// DAILY_TRADE_COUNT

TEST_CASE("the daily trade count cap is enforced", "[rule][trade_count]") {
    RiskContext context                       = BaselineContext();
    context.budgets.broker_max_trades_per_day = 10;
    context.trades_today                      = 10;
    RequireRejected(evaluate(BaselineBuy(), context), rules::kDailyTradeCount);

    context.trades_today = 9;
    RequireAllowed(evaluate(BaselineBuy(), context));
}

TEST_CASE("the daily trade count cap applies to sells", "[rule][trade_count]") {
    RiskContext context                       = BaselineContext();
    context.positions                         = {{"NVDA.US", Position{100, 90.0}}};
    context.budgets.broker_max_trades_per_day = 3;
    context.trades_today                      = 3;
    RequireRejected(evaluate(BaselineSell(10), context), rules::kDailyTradeCount);
}

// DAILY_TURNOVER

TEST_CASE("the daily turnover cap is enforced", "[rule][turnover]") {
    RiskContext context                         = BaselineContext();
    context.budgets.broker_max_turnover_per_day = 5'000.0;
    context.turnover_today                      = 4'500.0;  // + 1000 = 5500
    RequireRejected(evaluate(BaselineBuy(), context), rules::kDailyTurnover);
}

TEST_CASE("turnover landing exactly on the cap is allowed", "[rule][turnover]") {
    RiskContext context                         = BaselineContext();
    context.budgets.broker_max_turnover_per_day = 5'000.0;
    context.turnover_today                      = 4'000.0;  // + 1000 = 5000
    RequireAllowed(evaluate(BaselineBuy(), context));
}

TEST_CASE("the daily turnover cap applies to sells", "[rule][turnover]") {
    RiskContext context                         = BaselineContext();
    context.positions                           = {{"NVDA.US", Position{100, 90.0}}};
    context.budgets.broker_max_turnover_per_day = 5'000.0;
    context.turnover_today                      = 4'500.0;
    RequireRejected(evaluate(BaselineSell(10), context), rules::kDailyTurnover);
}

// Purity and preconditions

TEST_CASE("evaluate does not mutate its context", "[purity]") {
    RiskContext context = BaselineContext();
    context.positions   = {{"NVDA.US", Position{10, 90.0}}};

    const double cash_before      = context.account.cash;
    const int    trades_before    = context.trades_today;
    const double turnover_before  = context.turnover_today;
    const auto   positions_before = context.positions;
    const auto   seen_before      = context.seen_client_order_ids;

    RequireAllowed(evaluate(BaselineBuy(), context));

    REQUIRE(context.account.cash == cash_before);
    REQUIRE(context.trades_today == trades_before);
    REQUIRE(context.turnover_today == turnover_before);
    REQUIRE(context.positions == positions_before);
    REQUIRE(context.seen_client_order_ids == seen_before);
}

TEST_CASE("evaluate is deterministic across repeated calls", "[purity]") {
    const RiskContext        context     = BaselineContext();
    const TradingInstruction instruction = BaselineBuy(26, 100.0);

    const RiskDecision first  = evaluate(instruction, context);
    const RiskDecision second = evaluate(instruction, context);
    REQUIRE(first.verdict == second.verdict);
    REQUIRE(first.rule_id == second.rule_id);
    REQUIRE(first.reason == second.reason);
}

TEST_CASE("evaluate refuses an unfinalized context", "[purity]") {
    RiskContext context;  // never Finalize()d
    REQUIRE_THROWS_AS(evaluate(BaselineBuy(), context), RiskConfigError);
}
