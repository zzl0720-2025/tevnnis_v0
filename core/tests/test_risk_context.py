"""Test context assembly and sequential batch evaluation through Python.

Rule-level coverage lives in risk/tests/ and test_risk_engine.py.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tevnnis_core.instructions import Action, OrderType, TradingInstruction
from tevnnis_core.ports import AccountSnapshot, PositionSnapshot
from tevnnis_core.risk_context import RiskActivity, build_risk_context, check_batch

risk = pytest.importorskip(
    "tevnnis_risk",
    reason="build the extension first: cmake -S . -B build && cmake --build build -j",
)

TZ = "America/New_York"


def order(
    *,
    action: Action = Action.BUY,
    symbol: str = "NVDA.US",
    quantity: int = 10,
    limit_price: float = 100.0,
    client_order_id: str = "co_1",
) -> TradingInstruction:
    return TradingInstruction(
        action=action,
        symbol=symbol,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price=limit_price,
        valid_seconds=300,
        confidence=0.8,
        client_order_id=client_order_id,
    )


def context(
    config,
    now,
    *,
    cash: float = 10_000.0,
    positions: list[PositionSnapshot] | None = None,
    last_prices: dict[str, float] | None = None,
    symbol_status: dict[str, str] | None = None,
    activity: RiskActivity | None = None,
    kill_switch: bool = False,
):
    return build_risk_context(
        config,
        account=AccountSnapshot(buying_power=cash, cash=cash, net_liquidation=cash),
        positions=positions or [],
        last_prices=last_prices or {"NVDA.US": 100.0, "AMD.US": 50.0, "XOM.US": 100.0},
        symbol_status=symbol_status or {},
        activity=activity or RiskActivity(),
        now=now,
        kill_switch=kill_switch,
        risk=risk,
    )


def test_context_carries_config_universe_limits_and_managed_capital(config, now):
    built = context(config, now)

    assert built.finalized
    assert built.in_universe("NVDA.US")
    assert not built.in_universe("TSLA.US")
    assert built.sector_of("NVDA.US") == "Semiconductor"
    assert built.account.managed_capital == config.account.managed_capital
    assert built.limits.max_order_notional == config.risk_limits.max_order_notional


def test_promoted_no_trade_window_knobs_reach_the_engine(config, now):
    """Pass configured opening and closing windows to the engine."""
    config.risk_limits.no_trade_after_open_minutes = 17
    config.risk_limits.no_trade_before_close_minutes = 23
    built = context(config, now)

    assert built.limits.no_trade_after_open_minutes == 17
    assert built.limits.no_trade_before_close_minutes == 23


def test_session_and_window_timestamps_come_from_the_configured_timezone(config, now):
    built = context(config, now)
    assert built.session == risk.MarketSession.OPEN
    assert built.market_open_ts < built.now_epoch_s < built.market_close_ts


def test_a_closed_market_rejects_every_order(config):
    evening = datetime(2026, 9, 2, 21, 0, tzinfo=ZoneInfo(TZ))
    verdicts = check_batch([order()], context(config, evening), risk=risk)
    assert verdicts[0].rule_id == "NO_TRADE_WINDOW"


def test_halted_symbol_is_rejected(config, now):
    built = context(config, now, symbol_status={"NVDA.US": "HALTED"})
    assert check_batch([order()], built, risk=risk)[0].rule_id == "SYMBOL_HALTED"


def test_symbol_outside_the_universe_is_rejected_on_entry(config, now):
    built = context(config, now, last_prices={"TSLA.US": 100.0})
    verdicts = check_batch([order(symbol="TSLA.US")], built, risk=risk)
    assert verdicts[0].rule_id == "UNIVERSE_ALLOWLIST"


def test_an_already_used_client_order_id_is_rejected(config, now):
    built = context(config, now, activity=RiskActivity(seen_client_order_ids={"co_1"}))
    assert check_batch([order()], built, risk=risk)[0].rule_id == "DUPLICATE_ORDER"


def test_todays_activity_from_the_db_reaches_the_fee_gates(config, now):
    spent = RiskActivity(
        trades_today=config.budgets.broker_max_trades_per_day,
        turnover_today=0.0,
    )
    verdicts = check_batch([order()], context(config, now, activity=spent), risk=risk)
    assert verdicts[0].rule_id == "DAILY_TRADE_COUNT"


def test_kill_switch_outranks_everything(config, now):
    built = context(config, now, kill_switch=True)
    assert check_batch([order()], built, risk=risk)[0].rule_id == "KILL_SWITCH"


def test_hold_is_allowed_as_a_no_op(config, now):
    hold = TradingInstruction(
        action=Action.HOLD,
        symbol="NVDA.US",
        quantity=0,
        limit_price=0.0,
        valid_seconds=0,
        confidence=0.5,
        client_order_id="co_hold",
    )
    verdict = check_batch([hold], context(config, now), risk=risk)[0]
    assert verdict.allowed
    assert verdict.rule_id == "HOLD_NO_OP"


# sequential provisional state


def test_two_buys_that_jointly_breach_cash_reject_the_second_not_the_first(config, now):
    """Each fits alone; together they do not. The batch must not use a stale snapshot."""
    built = context(config, now, cash=2_500.0)
    verdicts = check_batch(
        [
            order(quantity=15, limit_price=100.0, client_order_id="co_1"),
            order(quantity=15, limit_price=100.0, client_order_id="co_2"),
        ],
        built,
        risk=risk,
    )

    assert verdicts[0].allowed
    assert not verdicts[1].allowed
    assert verdicts[1].rule_id in ("BUYING_POWER", "MIN_CASH_RESERVE")


def test_a_rejected_instruction_consumes_no_provisional_budget(config, now):
    built = context(config, now, cash=10_000.0)
    verdicts = check_batch(
        [
            order(symbol="TSLA.US", client_order_id="co_1"),  # rejected: not in universe
            order(symbol="NVDA.US", client_order_id="co_2"),  # must still be allowed
        ],
        built,
        risk=risk,
    )

    assert not verdicts[0].allowed
    assert verdicts[1].allowed


def test_the_callers_context_is_not_mutated_by_a_batch(config, now):
    built = context(config, now, cash=10_000.0)
    check_batch([order(quantity=10, limit_price=100.0)], built, risk=risk)
    assert built.account.cash == 10_000.0
    assert built.trades_today == 0
