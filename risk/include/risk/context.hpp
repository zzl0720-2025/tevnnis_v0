#pragma once
// Inputs supplied by core for deterministic risk evaluation.
// Broker state, market data, configuration and time are passed explicitly.

#include <cstdint>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace tevnnis::risk {

// Malformed or unfinalized contexts are programming/configuration errors,
// not ordinary trade rejections.
class RiskConfigError : public std::runtime_error {
public:
    explicit RiskConfigError(const std::string& what) : std::runtime_error(what) {}
};

// Where the wall clock sits relative to the trading day (no-trade windows).
enum class MarketSession : int {
    kClosed     = 0,
    kPreMarket  = 1,
    kOpen       = 2,
    kPostMarket = 3,
};

// Per-symbol trading status as reported by md (StatusPayload).
enum class SymbolStatus : int {
    kNormal = 0,
    kHalted = 1,
};

// One holding. avg_cost is the per-share basis; valuation uses last price.
struct Position {
    std::int64_t quantity = 0;
    double       avg_cost = 0.0;

    friend bool operator==(const Position& lhs, const Position& rhs) {
        return lhs.quantity == rhs.quantity && lhs.avg_cost == rhs.avg_cost;
    }
    friend bool operator!=(const Position& lhs, const Position& rhs) { return !(lhs == rhs); }
};

// Account snapshot. Mirrors core.ports.AccountSnapshot plus
// account.managed_capital, which is the denominator for the percentage caps.
struct AccountState {
    double cash            = 0.0;
    double buying_power    = 0.0;
    double net_liquidation = 0.0;
    double managed_capital = 0.0;
};

// Defaults match config/config.example.yaml.
// Core supplies configured limits, including the opening and closing windows.
struct RiskLimits {
    double max_position_pct              = 0.25;
    double max_sector_pct                = 0.50;
    double min_cash_reserve_pct          = 0.10;
    double limit_price_max_deviation_pct = 0.03;
    double max_order_notional            = 3000.0;
    int    max_day_trades_per_week       = 3;
    int    no_trade_after_open_minutes   = 5;
    int    no_trade_before_close_minutes = 5;
};

// The broker subset of budgets. The llm_* budgets are the budget guard's
// concern, not Risk's.
struct Budgets {
    int    broker_max_trades_per_day   = 10;
    double broker_max_turnover_per_day = 5000.0;
};

// Everything the checks read. Copyable by design: EvaluateBatch takes it
// by value and advances a provisional copy across a multi-instruction decision.
class RiskContext {
public:
    // account / portfolio
    AccountState                     account;
    std::map<std::string, Position>  positions;      // symbol -> holding

    // market snapshot
    std::map<std::string, double>        last_prices;    // symbol -> last price
    std::map<std::string, SymbolStatus>  symbol_status;  // absent => kNormal

    // configured universe: sector -> symbols
    // This map is both the allowlist and the sector classification table.
    std::map<std::string, std::vector<std::string>> universe;

    // clock / session
    std::int64_t  now_epoch_s    = 0;
    MarketSession session        = MarketSession::kClosed;
    std::int64_t  market_open_ts = 0;  // 0 => open-window check skipped
    std::int64_t  market_close_ts = 0; // 0 => close-window check skipped

    // configured limits
    RiskLimits limits;
    Budgets    budgets;

    // idempotency
    std::set<std::string> seen_client_order_ids;  // open + recently seen

    // today's activity (fee gates)
    int    trades_today   = 0;
    double turnover_today = 0.0;

    // PDT state
    int                   day_trades_this_week = 0;
    std::set<std::string> positions_opened_today;  // symbol bought today

    // global stop
    bool kill_switch_engaged = false;

    // Validates the context and builds the symbol -> sector index.
    // Must be called before evaluate(); throws RiskConfigError on bad input.
    void Finalize();

    bool finalized() const { return finalized_; }

    // Universe allowlist membership. Requires Finalize().
    bool InUniverse(const std::string& symbol) const;

    // Sector for a symbol, or nullptr if the symbol is outside the universe.
    const std::string* SectorOf(const std::string& symbol) const;

    // All symbols classified into `sector` (empty if unknown sector).
    const std::vector<std::string>& SymbolsInSector(const std::string& sector) const;

    const std::map<std::string, std::string>& symbol_sector() const { return symbol_sector_; }

    // Status of a symbol; symbols with no reported status are kNormal.
    SymbolStatus StatusOf(const std::string& symbol) const;

    // Held share count for a symbol (0 if not held).
    std::int64_t HeldQuantity(const std::string& symbol) const;

    // Last price for a symbol, or nullptr if md has never priced it.
    const double* LastPrice(const std::string& symbol) const;

private:
    std::map<std::string, std::string> symbol_sector_;
    bool                               finalized_ = false;
};

}  // namespace tevnnis::risk
