#pragma once
// Shared fixtures for the risk tests.
//
// BaselineContext() is deliberately permissive: every check passes for
// BaselineBuy(). Each test then breaks exactly one input, so a reject can only
// be attributed to the rule under test.

#include <string>

#include "risk/context.hpp"
#include "risk/engine.hpp"
#include "risk/instruction.hpp"

namespace tevnnis::risk::test {

// A fixed, arbitrary trading day. 09:30 -> 16:00 is 6.5h = 23400s.
inline constexpr std::int64_t kMarketOpenTs  = 1'700'000'000;
inline constexpr std::int64_t kMarketCloseTs = kMarketOpenTs + 23'400;
inline constexpr std::int64_t kMidSessionTs  = kMarketOpenTs + 3'600;

inline RiskContext BaselineContext() {
    RiskContext context;

    context.account.cash            = 10'000.0;
    context.account.buying_power    = 10'000.0;
    context.account.net_liquidation = 10'000.0;
    context.account.managed_capital = 10'000.0;

    context.universe = {
        {"Semiconductor", {"NVDA.US", "AMD.US"}},
        {"Energy", {"XOM.US"}},
        {"Web", {"GOOGL.US", "META.US"}},
    };

    context.last_prices = {
        {"NVDA.US", 100.0},
        {"AMD.US", 50.0},
        {"XOM.US", 100.0},
        {"GOOGL.US", 200.0},
        {"META.US", 400.0},
    };

    context.session         = MarketSession::kOpen;
    context.market_open_ts  = kMarketOpenTs;
    context.market_close_ts = kMarketCloseTs;
    context.now_epoch_s     = kMidSessionTs;

    // defaults, matching config/config.example.yaml.
    context.limits  = RiskLimits{};
    context.budgets = Budgets{};

    context.Finalize();
    return context;
}

// 10 NVDA @ 100 = 1000 notional: inside every baseline limit.
inline TradingInstruction BaselineBuy(std::int64_t quantity = 10, double limit_price = 100.0,
                                      std::string client_order_id = "coid-buy-1") {
    TradingInstruction instruction;
    instruction.action          = Action::kBuy;
    instruction.symbol          = "NVDA.US";
    instruction.order_type      = OrderType::kLimit;
    instruction.quantity        = quantity;
    instruction.limit_price     = limit_price;
    instruction.valid_seconds   = 300;
    instruction.confidence      = 0.8;
    instruction.client_order_id = std::move(client_order_id);
    return instruction;
}

// Requires the caller to seed a holding first.
inline TradingInstruction BaselineSell(std::int64_t quantity = 10, double limit_price = 100.0,
                                       std::string client_order_id = "coid-sell-1") {
    TradingInstruction instruction = BaselineBuy(quantity, limit_price, std::move(client_order_id));
    instruction.action             = Action::kSell;
    return instruction;
}

inline TradingInstruction BaselineHold(std::string client_order_id = "coid-hold-1") {
    TradingInstruction instruction;
    instruction.action          = Action::kHold;
    instruction.symbol          = "NVDA.US";
    instruction.confidence      = 0.5;
    instruction.client_order_id = std::move(client_order_id);
    return instruction;
}

}  // namespace tevnnis::risk::test
