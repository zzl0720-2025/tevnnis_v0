// In-process Python bindings for the C++ risk engine.
// STL casters return copies: assign whole containers rather than mutating
// a temporary such as context.positions["NVDA.US"].

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <optional>
#include <sstream>

#include "risk/context.hpp"
#include "risk/engine.hpp"
#include "risk/instruction.hpp"

namespace py = pybind11;
using namespace tevnnis::risk;

PYBIND11_MODULE(tevnnis_risk, m) {
    m.doc() =
        "TEVNNIS Risk Engine: deterministic evaluation with explicit inputs.\n"
        "evaluate(instruction, context) -> RiskDecision.\n"
        "Container attributes are copied across the binding: assign whole "
        "dicts/sets rather than mutating them in place.";

    py::register_exception<RiskConfigError>(m, "RiskConfigError", PyExc_ValueError);

    // enums (names mirror core.instructions / proto)
    py::enum_<Action>(m, "Action")
        .value("HOLD", Action::kHold)
        .value("BUY", Action::kBuy)
        .value("SELL", Action::kSell);

    py::enum_<OrderType>(m, "OrderType").value("LIMIT", OrderType::kLimit);

    py::enum_<MarketSession>(m, "MarketSession")
        .value("CLOSED", MarketSession::kClosed)
        .value("PRE_MARKET", MarketSession::kPreMarket)
        .value("OPEN", MarketSession::kOpen)
        .value("POST_MARKET", MarketSession::kPostMarket);

    py::enum_<SymbolStatus>(m, "SymbolStatus")
        .value("NORMAL", SymbolStatus::kNormal)
        .value("HALTED", SymbolStatus::kHalted);

    py::enum_<Verdict>(m, "Verdict")
        .value("ALLOW", Verdict::kAllow)
        .value("REJECT", Verdict::kReject);

    // canonical instruction
    py::class_<TradingInstruction>(m, "TradingInstruction")
        .def(py::init([](Action action, std::string symbol, OrderType order_type,
                         std::int64_t quantity, double limit_price, std::int32_t valid_seconds,
                         double confidence, std::string client_order_id) {
                 TradingInstruction instruction;
                 instruction.action = action;
                 instruction.symbol = std::move(symbol);
                 instruction.order_type = order_type;
                 instruction.quantity = quantity;
                 instruction.limit_price = limit_price;
                 instruction.valid_seconds = valid_seconds;
                 instruction.confidence = confidence;
                 instruction.client_order_id = std::move(client_order_id);
                 return instruction;
             }),
             py::kw_only(), py::arg("action") = Action::kHold, py::arg("symbol") = std::string(),
             py::arg("order_type") = OrderType::kLimit, py::arg("quantity") = 0,
             py::arg("limit_price") = 0.0, py::arg("valid_seconds") = 0,
             py::arg("confidence") = 0.0, py::arg("client_order_id") = std::string())
        .def_readwrite("action", &TradingInstruction::action)
        .def_readwrite("symbol", &TradingInstruction::symbol)
        .def_readwrite("order_type", &TradingInstruction::order_type)
        .def_readwrite("quantity", &TradingInstruction::quantity)
        .def_readwrite("limit_price", &TradingInstruction::limit_price)
        .def_readwrite("valid_seconds", &TradingInstruction::valid_seconds)
        .def_readwrite("confidence", &TradingInstruction::confidence)
        .def_readwrite("client_order_id", &TradingInstruction::client_order_id)
        .def("__repr__", [](const TradingInstruction& self) {
            std::ostringstream out;
            out << "<TradingInstruction action="
                << (self.action == Action::kBuy    ? "BUY"
                    : self.action == Action::kSell ? "SELL"
                                                   : "HOLD")
                << " symbol='" << self.symbol << "' quantity=" << self.quantity
                << " limit_price=" << self.limit_price << " client_order_id='"
                << self.client_order_id << "'>";
            return out.str();
        });

    m.def("order_notional", &OrderNotional, py::arg("instruction"),
          "quantity * limit_price for the given instruction.");

    // context building blocks
    py::class_<Position>(m, "Position")
        .def(py::init([](std::int64_t quantity, double avg_cost) {
                 return Position{quantity, avg_cost};
             }),
             py::kw_only(), py::arg("quantity") = 0, py::arg("avg_cost") = 0.0)
        .def_readwrite("quantity", &Position::quantity)
        .def_readwrite("avg_cost", &Position::avg_cost)
        .def("__repr__", [](const Position& self) {
            std::ostringstream out;
            out << "<Position quantity=" << self.quantity << " avg_cost=" << self.avg_cost << ">";
            return out.str();
        });

    py::class_<AccountState>(m, "AccountState")
        .def(py::init([](double cash, double buying_power, double net_liquidation,
                         double managed_capital) {
                 return AccountState{cash, buying_power, net_liquidation, managed_capital};
             }),
             py::kw_only(), py::arg("cash") = 0.0, py::arg("buying_power") = 0.0,
             py::arg("net_liquidation") = 0.0, py::arg("managed_capital") = 0.0)
        .def_readwrite("cash", &AccountState::cash)
        .def_readwrite("buying_power", &AccountState::buying_power)
        .def_readwrite("net_liquidation", &AccountState::net_liquidation)
        .def_readwrite("managed_capital", &AccountState::managed_capital);

    py::class_<RiskLimits>(m, "RiskLimits")
        .def(py::init([](double max_position_pct, double max_sector_pct,
                         double min_cash_reserve_pct, double limit_price_max_deviation_pct,
                         double max_order_notional, int max_day_trades_per_week,
                         int no_trade_after_open_minutes, int no_trade_before_close_minutes) {
                 RiskLimits limits;
                 limits.max_position_pct = max_position_pct;
                 limits.max_sector_pct = max_sector_pct;
                 limits.min_cash_reserve_pct = min_cash_reserve_pct;
                 limits.limit_price_max_deviation_pct = limit_price_max_deviation_pct;
                 limits.max_order_notional = max_order_notional;
                 limits.max_day_trades_per_week = max_day_trades_per_week;
                 limits.no_trade_after_open_minutes = no_trade_after_open_minutes;
                 limits.no_trade_before_close_minutes = no_trade_before_close_minutes;
                 return limits;
             }),
             py::kw_only(), py::arg("max_position_pct") = RiskLimits{}.max_position_pct,
             py::arg("max_sector_pct") = RiskLimits{}.max_sector_pct,
             py::arg("min_cash_reserve_pct") = RiskLimits{}.min_cash_reserve_pct,
             py::arg("limit_price_max_deviation_pct") =
                 RiskLimits{}.limit_price_max_deviation_pct,
             py::arg("max_order_notional") = RiskLimits{}.max_order_notional,
             py::arg("max_day_trades_per_week") = RiskLimits{}.max_day_trades_per_week,
             py::arg("no_trade_after_open_minutes") = RiskLimits{}.no_trade_after_open_minutes,
             py::arg("no_trade_before_close_minutes") =
                 RiskLimits{}.no_trade_before_close_minutes)
        .def_readwrite("max_position_pct", &RiskLimits::max_position_pct)
        .def_readwrite("max_sector_pct", &RiskLimits::max_sector_pct)
        .def_readwrite("min_cash_reserve_pct", &RiskLimits::min_cash_reserve_pct)
        .def_readwrite("limit_price_max_deviation_pct",
                       &RiskLimits::limit_price_max_deviation_pct)
        .def_readwrite("max_order_notional", &RiskLimits::max_order_notional)
        .def_readwrite("max_day_trades_per_week", &RiskLimits::max_day_trades_per_week)
        .def_readwrite("no_trade_after_open_minutes", &RiskLimits::no_trade_after_open_minutes)
        .def_readwrite("no_trade_before_close_minutes",
                       &RiskLimits::no_trade_before_close_minutes);

    py::class_<Budgets>(m, "Budgets")
        .def(py::init([](int broker_max_trades_per_day, double broker_max_turnover_per_day) {
                 return Budgets{broker_max_trades_per_day, broker_max_turnover_per_day};
             }),
             py::kw_only(),
             py::arg("broker_max_trades_per_day") = Budgets{}.broker_max_trades_per_day,
             py::arg("broker_max_turnover_per_day") = Budgets{}.broker_max_turnover_per_day)
        .def_readwrite("broker_max_trades_per_day", &Budgets::broker_max_trades_per_day)
        .def_readwrite("broker_max_turnover_per_day", &Budgets::broker_max_turnover_per_day);

    // RiskContext
    py::class_<RiskContext>(m, "RiskContext")
        .def(py::init<>())
        .def_readwrite("account", &RiskContext::account)
        .def_readwrite("positions", &RiskContext::positions)
        .def_readwrite("last_prices", &RiskContext::last_prices)
        .def_readwrite("symbol_status", &RiskContext::symbol_status)
        .def_readwrite("universe", &RiskContext::universe)
        .def_readwrite("now_epoch_s", &RiskContext::now_epoch_s)
        .def_readwrite("session", &RiskContext::session)
        .def_readwrite("market_open_ts", &RiskContext::market_open_ts)
        .def_readwrite("market_close_ts", &RiskContext::market_close_ts)
        .def_readwrite("limits", &RiskContext::limits)
        .def_readwrite("budgets", &RiskContext::budgets)
        .def_readwrite("seen_client_order_ids", &RiskContext::seen_client_order_ids)
        .def_readwrite("trades_today", &RiskContext::trades_today)
        .def_readwrite("turnover_today", &RiskContext::turnover_today)
        .def_readwrite("day_trades_this_week", &RiskContext::day_trades_this_week)
        .def_readwrite("positions_opened_today", &RiskContext::positions_opened_today)
        .def_readwrite("kill_switch_engaged", &RiskContext::kill_switch_engaged)
        .def("finalize", &RiskContext::Finalize,
             "Validate the context and build the symbol->sector index. Must be "
             "called before evaluate(); raises RiskConfigError on bad input.")
        .def_property_readonly("finalized", &RiskContext::finalized)
        .def("in_universe", &RiskContext::InUniverse, py::arg("symbol"))
        .def(
            "sector_of",
            [](const RiskContext& self, const std::string& symbol) -> std::optional<std::string> {
                const std::string* sector = self.SectorOf(symbol);
                if (sector == nullptr) {
                    return std::nullopt;
                }
                return *sector;
            },
            py::arg("symbol"), "Sector for a symbol, or None if outside the universe.")
        .def("status_of", &RiskContext::StatusOf, py::arg("symbol"))
        .def("held_quantity", &RiskContext::HeldQuantity, py::arg("symbol"))
        .def("symbol_sector", &RiskContext::symbol_sector,
             "The full symbol -> sector index (built by finalize()).");

    // decision
    py::class_<RiskDecision>(m, "RiskDecision")
        .def_readonly("verdict", &RiskDecision::verdict)
        .def_readonly("reason", &RiskDecision::reason)
        .def_readonly("rule_id", &RiskDecision::rule_id)
        .def_property_readonly("allowed", &RiskDecision::allowed)
        .def("__repr__", [](const RiskDecision& self) {
            std::ostringstream out;
            out << "<RiskDecision " << (self.allowed() ? "ALLOW" : "REJECT") << " rule_id='"
                << self.rule_id << "' reason='" << self.reason << "'>";
            return out.str();
        });

    // the engine
    m.def("evaluate", &evaluate, py::arg("instruction"), py::arg("context"),
          "Evaluate an instruction against the supplied context. No I/O.");
    m.def("is_day_trade", &IsDayTrade, py::arg("instruction"), py::arg("context"),
          "True when this SELL closes a position opened today (consumes a PDT day trade).");
    m.def("apply_allowed", &ApplyAllowed, py::arg("instruction"), py::arg("context"),
          "Apply an allowed instruction's projected effects to the context in place.");
    m.def("evaluate_batch", &EvaluateBatch, py::arg("instructions"), py::arg("context"),
          "Evaluate a multi-instruction decision sequentially against provisional "
          "state. The caller's context is not mutated.");

    // stable rule ids (persisted to the risk_audit table)
    py::module_ rules_module = m.def_submodule("rules", "Stable rule identifiers.");
    rules_module.attr("ALLOWED") = rules::kAllowed;
    rules_module.attr("HOLD_NO_OP") = rules::kHoldNoOp;
    rules_module.attr("KILL_SWITCH") = rules::kKillSwitch;
    rules_module.attr("DUPLICATE_ORDER") = rules::kDuplicateOrder;
    rules_module.attr("UNIVERSE_ALLOWLIST") = rules::kUniverseAllowlist;
    rules_module.attr("SYMBOL_HALTED") = rules::kSymbolHalted;
    rules_module.attr("NO_TRADE_WINDOW") = rules::kNoTradeWindow;
    rules_module.attr("MISSING_LAST_PRICE") = rules::kMissingLastPrice;
    rules_module.attr("LIMIT_PRICE_DEVIATION") = rules::kLimitPriceDeviation;
    rules_module.attr("MAX_ORDER_NOTIONAL") = rules::kMaxOrderNotional;
    rules_module.attr("INSUFFICIENT_HOLDINGS") = rules::kInsufficientHoldings;
    rules_module.attr("PDT_DAY_TRADE_CAP") = rules::kPdtDayTradeCap;
    rules_module.attr("BUYING_POWER") = rules::kBuyingPower;
    rules_module.attr("MIN_CASH_RESERVE") = rules::kMinCashReserve;
    rules_module.attr("MAX_POSITION_PCT") = rules::kMaxPositionPct;
    rules_module.attr("MAX_SECTOR_PCT") = rules::kMaxSectorPct;
    rules_module.attr("DAILY_TRADE_COUNT") = rules::kDailyTradeCount;
    rules_module.attr("DAILY_TURNOVER") = rules::kDailyTurnover;
}
