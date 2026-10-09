#pragma once
// Deterministic risk evaluation with explicit inputs and no I/O.
// Limits are supplied by core configuration, not by model output.

#include <string>
#include <vector>

#include "risk/context.hpp"
#include "risk/instruction.hpp"

namespace tevnnis::risk {

enum class Verdict : int {
    kAllow  = 0,
    kReject = 1,
};

// One decision. rule_id is the stable identifier persisted to the
// `risk_audit` table; reason is human-readable and carries the numbers.
struct RiskDecision {
    Verdict     verdict = Verdict::kReject;
    std::string reason;
    std::string rule_id;

    bool allowed() const { return verdict == Verdict::kAllow; }
};

// Stable audit identifiers. Evaluation returns the first applicable failure;
// checks that do not apply to the instruction are skipped.
namespace rules {

inline constexpr const char* kAllowed             = "ALLOWED";
inline constexpr const char* kHoldNoOp            = "HOLD_NO_OP";
inline constexpr const char* kKillSwitch          = "KILL_SWITCH";
inline constexpr const char* kDuplicateOrder      = "DUPLICATE_ORDER";
inline constexpr const char* kUniverseAllowlist   = "UNIVERSE_ALLOWLIST";
inline constexpr const char* kSymbolHalted        = "SYMBOL_HALTED";
inline constexpr const char* kNoTradeWindow       = "NO_TRADE_WINDOW";
inline constexpr const char* kMissingLastPrice    = "MISSING_LAST_PRICE";
inline constexpr const char* kLimitPriceDeviation = "LIMIT_PRICE_DEVIATION";
inline constexpr const char* kMaxOrderNotional    = "MAX_ORDER_NOTIONAL";
inline constexpr const char* kInsufficientHoldings = "INSUFFICIENT_HOLDINGS";
inline constexpr const char* kPdtDayTradeCap      = "PDT_DAY_TRADE_CAP";
inline constexpr const char* kBuyingPower         = "BUYING_POWER";
inline constexpr const char* kMinCashReserve      = "MIN_CASH_RESERVE";
inline constexpr const char* kMaxPositionPct      = "MAX_POSITION_PCT";
inline constexpr const char* kMaxSectorPct        = "MAX_SECTOR_PCT";
inline constexpr const char* kDailyTradeCount     = "DAILY_TRADE_COUNT";
inline constexpr const char* kDailyTurnover       = "DAILY_TURNOVER";

}  // namespace rules

// Requires a finalized context. Universe membership and a reference price
// are required for BUY only. SELL still passes the other applicable checks,
// including price deviation when a reference price is available.
RiskDecision evaluate(const TradingInstruction& instruction, const RiskContext& context);

// True when this SELL closes (part of) a position opened today: i.e. it
// consumes one PDT day trade.
bool IsDayTrade(const TradingInstruction& instruction, const RiskContext& context);

// Apply projected order effects to the batch context. HOLD is a no-op.
// These are provisional changes, not confirmed broker fills.
void ApplyAllowed(const TradingInstruction& instruction, RiskContext& context);

// Evaluate sequentially, updating a provisional context after each allowance.
// The caller's context is unchanged; projected effects are not confirmed fills.
std::vector<RiskDecision> EvaluateBatch(const std::vector<TradingInstruction>& instructions,
                                        RiskContext                           context);

}  // namespace tevnnis::risk
