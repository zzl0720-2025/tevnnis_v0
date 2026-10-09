#include "risk/engine.hpp"

#include <algorithm>
#include <cmath>
#include <iomanip>
#include <sstream>

namespace tevnnis::risk {
namespace {

// Tolerance for "exceeds a limit". A value exactly at its limit is allowed;
// only a genuine excess rejects. Keeps float noise from flipping a decision.
constexpr double kEps = 1e-9;

std::string Num(double value) {
    std::ostringstream out;
    out << std::setprecision(10) << value;
    return out.str();
}

RiskDecision Allow(const char* rule_id, std::string reason = "") {
    return RiskDecision{Verdict::kAllow, std::move(reason), rule_id};
}

RiskDecision Reject(const char* rule_id, std::string reason) {
    return RiskDecision{Verdict::kReject, std::move(reason), rule_id};
}

// Conservative per-share valuation for the symbol being traded: a buy may
// fill anywhere up to its limit price, so value the projected holding at
// whichever of last price / limit price is higher.
double ValuationPrice(double last_price, double limit_price) {
    return std::max(last_price, limit_price);
}

// Marks a held symbol to market. Falls back to avg_cost when md has never
// priced it: a held position must never be valued at zero.
double PositionValue(const RiskContext& context, const std::string& symbol) {
    auto it = context.positions.find(symbol);
    if (it == context.positions.end() || it->second.quantity == 0) {
        return 0.0;
    }
    const double* last = context.LastPrice(symbol);
    const double  price = (last != nullptr) ? *last : it->second.avg_cost;
    return static_cast<double>(it->second.quantity) * price;
}

}  // namespace

bool IsDayTrade(const TradingInstruction& instruction, const RiskContext& context) {
    return instruction.action == Action::kSell &&
           context.positions_opened_today.count(instruction.symbol) > 0;
}

RiskDecision evaluate(const TradingInstruction& instruction, const RiskContext& context) {
    if (!context.finalized()) {
        throw RiskConfigError("evaluate(): RiskContext::Finalize() was never called");
    }

    // 0. HOLD places no order, so nothing downstream can act on it. It is
    // first-class and must be logged, but there is nothing to permit.
    if (instruction.action == Action::kHold) {
        return Allow(rules::kHoldNoOp, "HOLD places no order");
    }

    // 1. Global kill switch: the one stop that outranks every other check.
    if (context.kill_switch_engaged) {
        return Reject(rules::kKillSwitch, "global kill switch is engaged; all trading halted");
    }

    // 2. Duplicate / idempotency. A missing key is treated the same way:
    // without it a retry cannot be deduplicated, so the order must not go.
    if (instruction.client_order_id.empty()) {
        return Reject(rules::kDuplicateOrder,
                      "client_order_id is empty; an order with no idempotency key "
                      "cannot be deduplicated on retry");
    }
    if (context.seen_client_order_ids.count(instruction.client_order_id) > 0) {
        return Reject(rules::kDuplicateOrder,
                      "client_order_id '" + instruction.client_order_id +
                          "' has already been submitted");
    }

    // 3. Universe allowlist: blocks hallucinated tickers and enforces
    // the ETF filter. The curated list IS the filter, but it governs what we
    // may ENTER, not what we may exit: a holding must always be closeable,
    // including after its symbol is dropped from the universe. A SELL is
    // already bounded by INSUFFICIENT_HOLDINGS, which permits selling only
    // what we actually hold.
    if (instruction.action == Action::kBuy && !context.InUniverse(instruction.symbol)) {
        return Reject(rules::kUniverseAllowlist,
                      "symbol '" + instruction.symbol + "' is not in the configured universe");
    }

    // 4. No-trade window: halted symbol.
    if (context.StatusOf(instruction.symbol) == SymbolStatus::kHalted) {
        return Reject(rules::kSymbolHalted,
                      "symbol '" + instruction.symbol + "' is halted");
    }

    // 5. No-trade window: session, plus the open/close volatility windows.
    if (context.session != MarketSession::kOpen) {
        return Reject(rules::kNoTradeWindow, "market session is not open");
    }
    if (context.market_open_ts > 0) {
        const std::int64_t open_until =
            context.market_open_ts + 60LL * context.limits.no_trade_after_open_minutes;
        if (context.now_epoch_s < open_until) {
            return Reject(rules::kNoTradeWindow,
                          "within the " +
                              std::to_string(context.limits.no_trade_after_open_minutes) +
                              "-minute opening volatility window");
        }
    }
    if (context.market_close_ts > 0) {
        const std::int64_t close_from =
            context.market_close_ts - 60LL * context.limits.no_trade_before_close_minutes;
        if (context.now_epoch_s >= close_from) {
            return Reject(rules::kNoTradeWindow,
                          "within the " +
                              std::to_string(context.limits.no_trade_before_close_minutes) +
                              "-minute closing volatility window");
        }
    }

    // 6. A reference price. An ENTRY requires one: without it the deviation
    // check cannot run and we would be buying blind. An EXIT does not: a
    // holding must not become unsellable because md has no fresh quote.
    const double* last_price_ptr = context.LastPrice(instruction.symbol);
    const bool    has_last_price = last_price_ptr != nullptr &&
                                std::isfinite(*last_price_ptr) && *last_price_ptr > 0.0;
    if (!has_last_price && instruction.action == Action::kBuy) {
        return Reject(rules::kMissingLastPrice,
                      "no usable last price for '" + instruction.symbol +
                          "'; limit-price deviation cannot be checked");
    }

    // 7. Limit-price deviation from last price. Applies to a SELL too whenever
    // a price is available: only its absence is tolerated, not a bad price.
    if (has_last_price) {
        const double last_price = *last_price_ptr;
        const double deviation  = std::fabs(instruction.limit_price - last_price) / last_price;
        if (deviation > context.limits.limit_price_max_deviation_pct + kEps) {
            return Reject(rules::kLimitPriceDeviation,
                          "limit price " + Num(instruction.limit_price) + " deviates " +
                              Num(deviation) + " from last price " + Num(last_price) +
                              "; max is " + Num(context.limits.limit_price_max_deviation_pct));
        }
    }

    // 8. Max order notional.
    const double notional = OrderNotional(instruction);
    if (notional > context.limits.max_order_notional + kEps) {
        return Reject(rules::kMaxOrderNotional,
                      "order notional " + Num(notional) + " exceeds max_order_notional " +
                          Num(context.limits.max_order_notional));
    }

    const std::int64_t held = context.HeldQuantity(instruction.symbol);

    if (instruction.action == Action::kSell) {
        // 9. Holdings sufficient to sell: v0 has no shorting.
        if (held < instruction.quantity) {
            return Reject(rules::kInsufficientHoldings,
                          "cannot sell " + std::to_string(instruction.quantity) + " of '" +
                              instruction.symbol + "'; only " + std::to_string(held) + " held");
        }

        // 10. PDT weekly day-trade cap.
        if (IsDayTrade(instruction, context) &&
            context.day_trades_this_week + 1 > context.limits.max_day_trades_per_week) {
            return Reject(rules::kPdtDayTradeCap,
                          "selling '" + instruction.symbol +
                              "' opened today would be day trade " +
                              std::to_string(context.day_trades_this_week + 1) +
                              " this week; max_day_trades_per_week is " +
                              std::to_string(context.limits.max_day_trades_per_week));
        }
    }

    if (instruction.action == Action::kBuy) {
        // Both are guaranteed on the BUY path: the allowlist check above
        // established universe membership, and rule 6 a usable last price.
        const std::string& sector     = *context.SectorOf(instruction.symbol);
        const double       last_price = *last_price_ptr;

        // 11. Buying power.
        if (notional > context.account.buying_power + kEps) {
            return Reject(rules::kBuyingPower,
                          "order notional " + Num(notional) + " exceeds buying power " +
                              Num(context.account.buying_power));
        }

        // 12. Minimum cash reserve.
        const double required_reserve =
            context.limits.min_cash_reserve_pct * context.account.managed_capital;
        const double cash_after = context.account.cash - notional;
        if (cash_after < required_reserve - kEps) {
            return Reject(rules::kMinCashReserve,
                          "cash after order " + Num(cash_after) +
                              " would fall below the required reserve " + Num(required_reserve) +
                              " (" + Num(context.limits.min_cash_reserve_pct) +
                              " of managed capital " + Num(context.account.managed_capital) + ")");
        }

        // 13. Per-symbol position cap, valued conservatively.
        const double valuation_price = ValuationPrice(last_price, instruction.limit_price);
        const double projected_symbol_value =
            static_cast<double>(held + instruction.quantity) * valuation_price;
        const double symbol_cap =
            context.limits.max_position_pct * context.account.managed_capital;
        if (projected_symbol_value > symbol_cap + kEps) {
            return Reject(rules::kMaxPositionPct,
                          "projected position in '" + instruction.symbol + "' of " +
                              Num(projected_symbol_value) + " exceeds the per-symbol cap " +
                              Num(symbol_cap) + " (" + Num(context.limits.max_position_pct) +
                              " of managed capital)");
        }

        // 14. Per-sector position cap: the rest of the sector marked to
        // market, plus this symbol's projected value.
        double projected_sector_value = projected_symbol_value;
        for (const auto& peer : context.SymbolsInSector(sector)) {
            if (peer != instruction.symbol) {
                projected_sector_value += PositionValue(context, peer);
            }
        }
        const double sector_cap = context.limits.max_sector_pct * context.account.managed_capital;
        if (projected_sector_value > sector_cap + kEps) {
            return Reject(rules::kMaxSectorPct,
                          "projected exposure to sector '" + sector + "' of " +
                              Num(projected_sector_value) + " exceeds the sector cap " +
                              Num(sector_cap) + " (" + Num(context.limits.max_sector_pct) +
                              " of managed capital)");
        }
    }

    // 15. Daily trade-count cap (fee control): applies to buys and sells.
    if (context.trades_today + 1 > context.budgets.broker_max_trades_per_day) {
        return Reject(rules::kDailyTradeCount,
                      "this would be trade " + std::to_string(context.trades_today + 1) +
                          " today; broker_max_trades_per_day is " +
                          std::to_string(context.budgets.broker_max_trades_per_day));
    }

    // 16. Daily turnover cap (fee control).
    const double projected_turnover = context.turnover_today + notional;
    if (projected_turnover > context.budgets.broker_max_turnover_per_day + kEps) {
        return Reject(rules::kDailyTurnover,
                      "projected turnover " + Num(projected_turnover) +
                          " exceeds broker_max_turnover_per_day " +
                          Num(context.budgets.broker_max_turnover_per_day));
    }

    return Allow(rules::kAllowed);
}

void ApplyAllowed(const TradingInstruction& instruction, RiskContext& context) {
    if (instruction.action == Action::kHold) {
        return;  // no order, no state change
    }

    const double notional = OrderNotional(instruction);
    const bool   day_trade = IsDayTrade(instruction, context);

    if (instruction.action == Action::kBuy) {
        context.account.cash -= notional;
        context.account.buying_power -= notional;

        Position&          position = context.positions[instruction.symbol];
        const std::int64_t new_quantity = position.quantity + instruction.quantity;
        if (new_quantity > 0) {
            position.avg_cost = (static_cast<double>(position.quantity) * position.avg_cost +
                                 static_cast<double>(instruction.quantity) *
                                     instruction.limit_price) /
                                static_cast<double>(new_quantity);
        }
        position.quantity = new_quantity;

        // A symbol bought today can be closed today: which is a day trade.
        context.positions_opened_today.insert(instruction.symbol);
    } else {
        context.account.cash += notional;
        context.account.buying_power += notional;

        Position& position = context.positions[instruction.symbol];
        position.quantity -= instruction.quantity;
        if (position.quantity <= 0) {
            context.positions.erase(instruction.symbol);
        }

        if (day_trade) {
            context.day_trades_this_week += 1;
        }
    }

    context.trades_today += 1;
    context.turnover_today += notional;
    context.seen_client_order_ids.insert(instruction.client_order_id);
}

std::vector<RiskDecision> EvaluateBatch(const std::vector<TradingInstruction>& instructions,
                                        RiskContext                           context) {
    std::vector<RiskDecision> decisions;
    decisions.reserve(instructions.size());
    for (const auto& instruction : instructions) {
        RiskDecision decision = evaluate(instruction, context);
        if (decision.allowed()) {
            ApplyAllowed(instruction, context);
        }
        decisions.push_back(std::move(decision));
    }
    return decisions;
}

}  // namespace tevnnis::risk
