#include "risk/context.hpp"

#include <cmath>

namespace tevnnis::risk {
namespace {

const std::vector<std::string>& EmptySymbolList() {
    static const std::vector<std::string> kEmpty;
    return kEmpty;
}

void RequireFinite(double value, const char* field) {
    if (!std::isfinite(value)) {
        throw RiskConfigError(std::string("RiskContext: ") + field + " must be finite");
    }
}

void RequirePct(double value, const char* field) {
    RequireFinite(value, field);
    if (value < 0.0 || value > 1.0) {
        throw RiskConfigError(std::string("RiskContext: ") + field + " must be within [0, 1]");
    }
}

}  // namespace

void RiskContext::Finalize() {
    if (universe.empty()) {
        throw RiskConfigError("RiskContext: universe must have at least one sector");
    }

    symbol_sector_.clear();
    for (const auto& [sector, symbols] : universe) {
        if (sector.empty()) {
            throw RiskConfigError("RiskContext: universe sector name must not be empty");
        }
        if (symbols.empty()) {
            throw RiskConfigError("RiskContext: sector '" + sector +
                                  "' must have at least one symbol");
        }
        for (const auto& symbol : symbols) {
            if (symbol.empty()) {
                throw RiskConfigError("RiskContext: sector '" + sector +
                                      "' contains an empty symbol");
            }
            // A symbol in two sectors would make the sector cap ambiguous.
            auto [it, inserted] = symbol_sector_.emplace(symbol, sector);
            if (!inserted && it->second != sector) {
                throw RiskConfigError("RiskContext: symbol '" + symbol +
                                      "' appears in two sectors ('" + it->second + "' and '" +
                                      sector + "')");
            }
        }
    }

    RequirePct(limits.max_position_pct, "limits.max_position_pct");
    RequirePct(limits.max_sector_pct, "limits.max_sector_pct");
    RequirePct(limits.min_cash_reserve_pct, "limits.min_cash_reserve_pct");
    RequirePct(limits.limit_price_max_deviation_pct, "limits.limit_price_max_deviation_pct");
    RequireFinite(limits.max_order_notional, "limits.max_order_notional");
    if (limits.max_order_notional <= 0.0) {
        throw RiskConfigError("RiskContext: limits.max_order_notional must be > 0");
    }
    if (limits.max_day_trades_per_week < 0) {
        throw RiskConfigError("RiskContext: limits.max_day_trades_per_week must be >= 0");
    }
    if (limits.no_trade_after_open_minutes < 0 || limits.no_trade_before_close_minutes < 0) {
        throw RiskConfigError("RiskContext: no-trade window minutes must be >= 0");
    }

    if (budgets.broker_max_trades_per_day < 0) {
        throw RiskConfigError("RiskContext: budgets.broker_max_trades_per_day must be >= 0");
    }
    RequireFinite(budgets.broker_max_turnover_per_day, "budgets.broker_max_turnover_per_day");
    if (budgets.broker_max_turnover_per_day < 0.0) {
        throw RiskConfigError("RiskContext: budgets.broker_max_turnover_per_day must be >= 0");
    }

    RequireFinite(account.cash, "account.cash");
    RequireFinite(account.buying_power, "account.buying_power");
    RequireFinite(account.net_liquidation, "account.net_liquidation");
    RequireFinite(account.managed_capital, "account.managed_capital");
    if (account.managed_capital <= 0.0) {
        throw RiskConfigError("RiskContext: account.managed_capital must be > 0");
    }

    if (market_open_ts > 0 && market_close_ts > 0 && market_close_ts <= market_open_ts) {
        throw RiskConfigError("RiskContext: market_close_ts must be after market_open_ts");
    }

    finalized_ = true;
}

bool RiskContext::InUniverse(const std::string& symbol) const {
    return symbol_sector_.find(symbol) != symbol_sector_.end();
}

const std::string* RiskContext::SectorOf(const std::string& symbol) const {
    auto it = symbol_sector_.find(symbol);
    return it == symbol_sector_.end() ? nullptr : &it->second;
}

const std::vector<std::string>& RiskContext::SymbolsInSector(const std::string& sector) const {
    auto it = universe.find(sector);
    return it == universe.end() ? EmptySymbolList() : it->second;
}

SymbolStatus RiskContext::StatusOf(const std::string& symbol) const {
    auto it = symbol_status.find(symbol);
    return it == symbol_status.end() ? SymbolStatus::kNormal : it->second;
}

std::int64_t RiskContext::HeldQuantity(const std::string& symbol) const {
    auto it = positions.find(symbol);
    return it == positions.end() ? 0 : it->second.quantity;
}

const double* RiskContext::LastPrice(const std::string& symbol) const {
    auto it = last_prices.find(symbol);
    return it == last_prices.end() ? nullptr : &it->second;
}

}  // namespace tevnnis::risk
