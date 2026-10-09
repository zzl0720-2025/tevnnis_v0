"""Assemble broker, market, configuration and activity inputs for the C++ engine.

This module maps types; the engine owns rule evaluation and provisional batch
state. The CMake-built extension is loaded lazily with a build hint on failure.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tevnnis_core.config import StrategyConfig
from tevnnis_core.instructions import Action, TradingInstruction
from tevnnis_core.market_hours import MarketSession, open_close_epochs, session_for
from tevnnis_core.ports import AccountSnapshot, PositionSnapshot

# Default CMake extension path, also used by tests/conftest.py.
# TEVNNIS_RISK_MODULE_DIR overrides it for out-of-tree builds.
_DEFAULT_BUILD_DIR = Path(__file__).resolve().parents[3] / "build" / "risk"

_BUILD_HINT = (
    "the C++ Risk Engine extension (tevnnis_risk) is not importable. Build it with:\n"
    "    cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug\n"
    "    cmake --build build -j\n"
    "or set TEVNNIS_RISK_MODULE_DIR to the directory holding tevnnis_risk."
)


class RiskEngineUnavailable(RuntimeError):
    """The risk extension could not be imported: trading must not start."""


def load_risk_module() -> Any:
    """Import `tevnnis_risk` (adding the cmake output dir if needed), or explain how to build it."""
    try:
        import tevnnis_risk
    except ImportError:
        override = os.environ.get("TEVNNIS_RISK_MODULE_DIR")
        candidate = Path(override) if override else _DEFAULT_BUILD_DIR
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
        try:
            import tevnnis_risk
        except ImportError as exc:
            raise RiskEngineUnavailable(_BUILD_HINT) from exc
    return tevnnis_risk


@dataclass
class RiskActivity:
    """The DB-derived half of the context (see db/repository.py for the conventions)."""

    seen_client_order_ids: set[str] = field(default_factory=set)
    trades_today: int = 0
    turnover_today: float = 0.0
    day_trades_this_week: int = 0
    positions_opened_today: set[str] = field(default_factory=set)


_SESSION_NAMES = {
    MarketSession.CLOSED: "CLOSED",
    MarketSession.PRE_MARKET: "PRE_MARKET",
    MarketSession.OPEN: "OPEN",
    MarketSession.POST_MARKET: "POST_MARKET",
}


def to_risk_instruction(instruction: TradingInstruction, risk: Any) -> Any:
    """Pydantic canonical instruction -> the engine's TradingInstruction."""
    action = {
        Action.HOLD: risk.Action.HOLD,
        Action.BUY: risk.Action.BUY,
        Action.SELL: risk.Action.SELL,
    }[instruction.action]
    return risk.TradingInstruction(
        action=action,
        symbol=instruction.symbol,
        order_type=risk.OrderType.LIMIT,
        quantity=instruction.quantity,
        limit_price=instruction.limit_price,
        valid_seconds=instruction.valid_seconds,
        confidence=instruction.confidence,
        client_order_id=instruction.client_order_id,
    )


def build_risk_context(
    config: StrategyConfig,
    *,
    account: AccountSnapshot,
    positions: list[PositionSnapshot],
    last_prices: dict[str, float],
    symbol_status: dict[str, str],
    activity: RiskActivity,
    now: datetime,
    kill_switch: bool = False,
    risk: Any | None = None,
) -> Any:
    """Assemble and finalize a RiskContext for `now`.

    pybind11's STL casters copy, so every container is assigned whole: never
    mutated in place through the binding.
    """
    risk = risk or load_risk_module()
    tz = config.cadence.timezone
    limits = config.risk_limits
    market_open_ts, market_close_ts = open_close_epochs(now, tz)

    context = risk.RiskContext()
    context.account = risk.AccountState(
        cash=account.cash,
        buying_power=account.buying_power,
        net_liquidation=account.net_liquidation,
        managed_capital=config.account.managed_capital,
    )
    context.positions = {
        p.symbol: risk.Position(quantity=p.quantity, avg_cost=p.cost_basis) for p in positions
    }
    context.last_prices = dict(last_prices)
    context.symbol_status = {
        symbol: (risk.SymbolStatus.HALTED if status == "HALTED" else risk.SymbolStatus.NORMAL)
        for symbol, status in symbol_status.items()
    }
    context.universe = {sector: list(symbols) for sector, symbols in config.universe.items()}

    context.now_epoch_s = int(now.timestamp())
    context.session = getattr(risk.MarketSession, _SESSION_NAMES[session_for(now, tz)])
    context.market_open_ts = market_open_ts
    context.market_close_ts = market_close_ts

    context.limits = risk.RiskLimits(
        max_position_pct=limits.max_position_pct,
        max_sector_pct=limits.max_sector_pct,
        min_cash_reserve_pct=limits.min_cash_reserve_pct,
        limit_price_max_deviation_pct=limits.limit_price_max_deviation_pct,
        max_order_notional=limits.max_order_notional,
        max_day_trades_per_week=limits.max_day_trades_per_week,
        no_trade_after_open_minutes=limits.no_trade_after_open_minutes,
        no_trade_before_close_minutes=limits.no_trade_before_close_minutes,
    )
    context.budgets = risk.Budgets(
        broker_max_trades_per_day=config.budgets.broker_max_trades_per_day,
        broker_max_turnover_per_day=config.budgets.broker_max_turnover_per_day,
    )

    context.seen_client_order_ids = set(activity.seen_client_order_ids)
    context.trades_today = activity.trades_today
    context.turnover_today = activity.turnover_today
    context.day_trades_this_week = activity.day_trades_this_week
    context.positions_opened_today = set(activity.positions_opened_today)
    context.kill_switch_engaged = kill_switch

    context.finalize()
    return context


@dataclass(frozen=True)
class RiskVerdict:
    """One instruction's verdict, in core-native form."""

    allowed: bool
    rule_id: str
    reason: str


def check_batch(
    instructions: list[TradingInstruction], context: Any, *, risk: Any | None = None
) -> list[RiskVerdict]:
    """sequential batch evaluation: provisional state advances in the engine."""
    risk = risk or load_risk_module()
    decisions = risk.evaluate_batch(
        [to_risk_instruction(i, risk) for i in instructions], context
    )
    return [
        RiskVerdict(allowed=d.allowed, rule_id=d.rule_id, reason=d.reason) for d in decisions
    ]
