#pragma once
// In-process instruction type exposed through pybind11.
// Keep shared fields aligned with proto/instruction.proto and
// core/src/tevnnis_core/instructions.py. Plain structs avoid a protobuf
// dependency in the risk extension.

#include <cstdint>
#include <string>

namespace tevnnis::risk {

// Mirrors proto Action. v0: no LLM-initiated CANCEL.
enum class Action : int {
    kHold = 0,
    kBuy  = 1,
    kSell = 2,
};

// Mirrors proto OrderType. v0: limit orders only.
enum class OrderType : int {
    kLimit = 0,
};

// Canonical instruction: what core hands to Risk and then to Execution.
// thesis / cited_event_ids are stripped to the DB before this point.
struct TradingInstruction {
    Action       action          = Action::kHold;
    std::string  symbol;
    OrderType    order_type      = OrderType::kLimit;
    std::int64_t quantity        = 0;
    double       limit_price     = 0.0;
    std::int32_t valid_seconds   = 0;
    double       confidence      = 0.0;
    std::string  client_order_id;  // idempotency key; assigned by core
};

// Notional value of the order as priced by the instruction itself.
inline double OrderNotional(const TradingInstruction& instruction) {
    return static_cast<double>(instruction.quantity) * instruction.limit_price;
}

}  // namespace tevnnis::risk
