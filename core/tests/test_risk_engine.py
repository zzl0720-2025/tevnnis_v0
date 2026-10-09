"""Verify Python bindings return the same verdicts and rule IDs as C++ tests.

Build the extension before running pytest; otherwise these tests are skipped:

    cmake -S. -B build -DCMAKE_BUILD_TYPE=Debug
    cmake --build build -j
    cd core && uv run pytest

Assign container attributes as a whole: pybind11's STL casters copy them.
Nested structs such as context.limits and context.account allow field updates.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

risk = pytest.importorskip(
    "tevnnis_risk",
    reason="build the extension first: cmake -S . -B build && cmake --build build -j",
)

# A fixed, arbitrary trading day: identical to risk/tests/test_support.hpp.
MARKET_OPEN_TS = 1_700_000_000
MARKET_CLOSE_TS = MARKET_OPEN_TS + 23_400
MID_SESSION_TS = MARKET_OPEN_TS + 3_600


def baseline_context() -> Any:
    """Mirror of test::BaselineContext(): every check passes."""
    context = risk.RiskContext()
    context.account = risk.AccountState(
        cash=10_000.0,
        buying_power=10_000.0,
        net_liquidation=10_000.0,
        managed_capital=10_000.0,
    )
    context.universe = {
        "Semiconductor": ["NVDA.US", "AMD.US"],
        "Energy": ["XOM.US"],
        "Web": ["GOOGL.US", "META.US"],
    }
    context.last_prices = {
        "NVDA.US": 100.0,
        "AMD.US": 50.0,
        "XOM.US": 100.0,
        "GOOGL.US": 200.0,
        "META.US": 400.0,
    }
    context.session = risk.MarketSession.OPEN
    context.market_open_ts = MARKET_OPEN_TS
    context.market_close_ts = MARKET_CLOSE_TS
    context.now_epoch_s = MID_SESSION_TS
    context.finalize()
    return context


def buy(
    symbol: str = "NVDA.US",
    quantity: int = 10,
    limit_price: float = 100.0,
    client_order_id: str = "coid-buy-1",
) -> Any:
    return risk.TradingInstruction(
        action=risk.Action.BUY,
        symbol=symbol,
        quantity=quantity,
        limit_price=limit_price,
        valid_seconds=300,
        confidence=0.8,
        client_order_id=client_order_id,
    )


def sell(
    symbol: str = "NVDA.US",
    quantity: int = 10,
    limit_price: float = 100.0,
    client_order_id: str = "coid-sell-1",
) -> Any:
    instruction = buy(symbol, quantity, limit_price, client_order_id)
    instruction.action = risk.Action.SELL
    return instruction


def hold(client_order_id: str = "coid-hold-1") -> Any:
    return risk.TradingInstruction(
        action=risk.Action.HOLD,
        symbol="NVDA.US",
        confidence=0.5,
        client_order_id=client_order_id,
    )


# One case per rule: the reject path and the allow path.
# Each mutator breaks exactly one input against the permissive baseline.

Case = tuple[str, Callable[[Any], Any], str]


def _kill_switch(context: Any) -> Any:
    context.kill_switch_engaged = True
    return buy()


def _duplicate(context: Any) -> Any:
    context.seen_client_order_ids = {"coid-buy-1"}
    return buy()


def _empty_order_id(context: Any) -> Any:
    return buy(client_order_id="")


def _outside_universe(context: Any) -> Any:
    context.last_prices = dict(context.last_prices, **{"TSLA.US": 100.0})
    return buy(symbol="TSLA.US")


def _sell_outside_universe_beyond_holdings(context: Any) -> Any:
    # The allowlist is skipped for an exit, but holdings still bound it.
    context.positions = {"TSLA.US": risk.Position(quantity=5, avg_cost=90.0)}
    context.last_prices = dict(context.last_prices, **{"TSLA.US": 100.0})
    return sell(symbol="TSLA.US")


def _sell_with_bad_deviation(context: Any) -> Any:
    # Only the ABSENCE of a price is tolerated on an exit, never a bad price.
    context.positions = {"NVDA.US": risk.Position(quantity=10, avg_cost=90.0)}
    return sell(limit_price=105.0)


def _halted(context: Any) -> Any:
    context.symbol_status = {"NVDA.US": risk.SymbolStatus.HALTED}
    return buy()


def _session_closed(context: Any) -> Any:
    context.session = risk.MarketSession.CLOSED
    return buy()


def _opening_window(context: Any) -> Any:
    context.now_epoch_s = MARKET_OPEN_TS + 120
    return buy()


def _closing_window(context: Any) -> Any:
    context.now_epoch_s = MARKET_CLOSE_TS - 120
    return buy()


def _unpriced(context: Any) -> Any:
    context.last_prices = {k: v for k, v in context.last_prices.items() if k != "NVDA.US"}
    return buy()


def _deviation(context: Any) -> Any:
    return buy(limit_price=105.0)


def _notional(context: Any) -> Any:
    context.limits.max_order_notional = 1_000.0
    return buy(quantity=11)


def _insufficient_holdings(context: Any) -> Any:
    context.positions = {"NVDA.US": risk.Position(quantity=5, avg_cost=90.0)}
    return sell()


def _pdt(context: Any) -> Any:
    context.positions = {"NVDA.US": risk.Position(quantity=10, avg_cost=100.0)}
    context.positions_opened_today = {"NVDA.US"}
    context.day_trades_this_week = 3
    context.limits.max_day_trades_per_week = 3
    return sell()


def _buying_power(context: Any) -> Any:
    context.account.buying_power = 500.0
    return buy()


def _cash_reserve(context: Any) -> Any:
    context.account.cash = 1_500.0
    return buy()


def _position_cap(context: Any) -> Any:
    return buy(quantity=26)


def _sector_cap(context: Any) -> Any:
    context.positions = {"AMD.US": risk.Position(quantity=80, avg_cost=45.0)}
    return buy(quantity=25)


def _trade_count(context: Any) -> Any:
    context.trades_today = 10
    return buy()


def _turnover(context: Any) -> Any:
    context.turnover_today = 4_500.0
    return buy()


REJECT_CASES: list[Case] = [
    ("kill_switch", _kill_switch, risk.rules.KILL_SWITCH),
    ("duplicate_order", _duplicate, risk.rules.DUPLICATE_ORDER),
    ("empty_client_order_id", _empty_order_id, risk.rules.DUPLICATE_ORDER),
    ("universe_allowlist", _outside_universe, risk.rules.UNIVERSE_ALLOWLIST),
    ("symbol_halted", _halted, risk.rules.SYMBOL_HALTED),
    ("session_closed", _session_closed, risk.rules.NO_TRADE_WINDOW),
    ("opening_window", _opening_window, risk.rules.NO_TRADE_WINDOW),
    ("closing_window", _closing_window, risk.rules.NO_TRADE_WINDOW),
    ("missing_last_price", _unpriced, risk.rules.MISSING_LAST_PRICE),
    ("limit_price_deviation", _deviation, risk.rules.LIMIT_PRICE_DEVIATION),
    ("max_order_notional", _notional, risk.rules.MAX_ORDER_NOTIONAL),
    ("insufficient_holdings", _insufficient_holdings, risk.rules.INSUFFICIENT_HOLDINGS),
    (
        "sell_outside_universe_beyond_holdings",
        _sell_outside_universe_beyond_holdings,
        risk.rules.INSUFFICIENT_HOLDINGS,
    ),
    ("sell_bad_deviation", _sell_with_bad_deviation, risk.rules.LIMIT_PRICE_DEVIATION),
    ("pdt_day_trade_cap", _pdt, risk.rules.PDT_DAY_TRADE_CAP),
    ("buying_power", _buying_power, risk.rules.BUYING_POWER),
    ("min_cash_reserve", _cash_reserve, risk.rules.MIN_CASH_RESERVE),
    ("max_position_pct", _position_cap, risk.rules.MAX_POSITION_PCT),
    ("max_sector_pct", _sector_cap, risk.rules.MAX_SECTOR_PCT),
    ("daily_trade_count", _trade_count, risk.rules.DAILY_TRADE_COUNT),
    ("daily_turnover", _turnover, risk.rules.DAILY_TURNOVER),
]


@pytest.mark.parametrize(
    ("mutate", "expected_rule"),
    [pytest.param(case[1], case[2], id=case[0]) for case in REJECT_CASES],
)
def test_every_rule_rejects(mutate: Callable[[Any], Any], expected_rule: str) -> None:
    context = baseline_context()
    instruction = mutate(context)

    decision = risk.evaluate(instruction, context)

    assert not decision.allowed
    assert decision.verdict == risk.Verdict.REJECT
    assert decision.rule_id == expected_rule
    assert decision.reason  # the risk_audit row must say why


def _allow_baseline_buy(context: Any) -> Any:
    return buy()


def _allow_at_deviation_limit(context: Any) -> Any:
    return buy(limit_price=103.0)


def _allow_at_notional_limit(context: Any) -> Any:
    context.limits.max_order_notional = 1_000.0
    return buy(quantity=10)


def _allow_exact_holdings(context: Any) -> Any:
    context.positions = {"NVDA.US": risk.Position(quantity=10, avg_cost=90.0)}
    return sell()


def _allow_sell_outside_universe(context: Any) -> Any:
    # A holding dropped from the universe must remain closeable.
    context.positions = {"TSLA.US": risk.Position(quantity=10, avg_cost=90.0)}
    context.last_prices = dict(context.last_prices, **{"TSLA.US": 100.0})
    return sell(symbol="TSLA.US")


def _allow_sell_unpriced(context: Any) -> Any:
    # An exit must not be blocked for lack of a fresh quote.
    context.positions = {"NVDA.US": risk.Position(quantity=10, avg_cost=90.0)}
    context.last_prices = {k: v for k, v in context.last_prices.items() if k != "NVDA.US"}
    return sell()


def _allow_day_trade_within_cap(context: Any) -> Any:
    context.positions = {"NVDA.US": risk.Position(quantity=10, avg_cost=100.0)}
    context.positions_opened_today = {"NVDA.US"}
    context.day_trades_this_week = 2
    context.limits.max_day_trades_per_week = 3
    return sell()


def _allow_at_buying_power(context: Any) -> Any:
    context.account.buying_power = 1_000.0
    return buy()


def _allow_at_cash_reserve(context: Any) -> Any:
    context.account.cash = 2_000.0
    return buy()


def _allow_at_position_cap(context: Any) -> Any:
    return buy(quantity=25)


def _allow_within_sector_cap(context: Any) -> Any:
    context.positions = {"AMD.US": risk.Position(quantity=20, avg_cost=45.0)}
    return buy(quantity=25)


def _allow_at_open_window_boundary(context: Any) -> Any:
    context.now_epoch_s = MARKET_OPEN_TS + 300
    return buy()


def _allow_at_trade_count_limit(context: Any) -> Any:
    context.trades_today = 9
    return buy()


def _allow_at_turnover_limit(context: Any) -> Any:
    context.turnover_today = 4_000.0
    return buy()


ALLOW_CASES: list[tuple[str, Callable[[Any], Any]]] = [
    ("baseline_buy", _allow_baseline_buy),
    ("deviation_at_limit", _allow_at_deviation_limit),
    ("notional_at_limit", _allow_at_notional_limit),
    ("sell_exact_holdings", _allow_exact_holdings),
    ("sell_outside_universe", _allow_sell_outside_universe),
    ("sell_unpriced", _allow_sell_unpriced),
    ("day_trade_within_cap", _allow_day_trade_within_cap),
    ("buying_power_exact", _allow_at_buying_power),
    ("cash_reserve_exact", _allow_at_cash_reserve),
    ("position_cap_exact", _allow_at_position_cap),
    ("within_sector_cap", _allow_within_sector_cap),
    ("open_window_boundary", _allow_at_open_window_boundary),
    ("trade_count_last_slot", _allow_at_trade_count_limit),
    ("turnover_exact", _allow_at_turnover_limit),
]


@pytest.mark.parametrize(
    "mutate", [pytest.param(case[1], id=case[0]) for case in ALLOW_CASES]
)
def test_allow_paths(mutate: Callable[[Any], Any]) -> None:
    context = baseline_context()
    instruction = mutate(context)

    decision = risk.evaluate(instruction, context)

    assert decision.allowed
    assert decision.verdict == risk.Verdict.ALLOW
    assert decision.rule_id == risk.rules.ALLOWED


# HOLD, precedence, purity


def test_hold_is_a_permitted_no_op() -> None:
    decision = risk.evaluate(hold(), baseline_context())
    assert decision.allowed
    assert decision.rule_id == risk.rules.HOLD_NO_OP


def test_hold_short_circuits_ahead_of_every_other_check() -> None:
    context = baseline_context()
    context.kill_switch_engaged = True
    context.session = risk.MarketSession.CLOSED

    instruction = hold()
    instruction.symbol = "NOTREAL.US"
    instruction.client_order_id = ""

    decision = risk.evaluate(instruction, context)
    assert decision.allowed
    assert decision.rule_id == risk.rules.HOLD_NO_OP


@pytest.mark.parametrize(
    ("first_break", "second_break", "expected_rule"),
    [
        ("kill_switch", "duplicate", risk.rules.KILL_SWITCH),
        ("duplicate", "universe", risk.rules.DUPLICATE_ORDER),
        ("universe", "halted", risk.rules.UNIVERSE_ALLOWLIST),
        ("halted", "session", risk.rules.SYMBOL_HALTED),
    ],
)
def test_the_earlier_rule_wins(first_break: str, second_break: str, expected_rule: str) -> None:
    context = baseline_context()
    instruction = buy()

    breaks = {first_break, second_break}
    if "kill_switch" in breaks:
        context.kill_switch_engaged = True
    if "duplicate" in breaks:
        context.seen_client_order_ids = {instruction.client_order_id}
    if "universe" in breaks:
        instruction.symbol = "TSLA.US"
    if "halted" in breaks:
        context.symbol_status = {instruction.symbol: risk.SymbolStatus.HALTED}
    if "session" in breaks:
        context.session = risk.MarketSession.CLOSED

    assert risk.evaluate(instruction, context).rule_id == expected_rule


def test_evaluate_does_not_mutate_the_context() -> None:
    context = baseline_context()
    cash_before = context.account.cash

    assert risk.evaluate(buy(), context).allowed

    assert context.account.cash == cash_before
    assert context.trades_today == 0
    assert context.turnover_today == 0.0
    assert context.positions == {}
    assert context.seen_client_order_ids == set()


def test_evaluate_is_deterministic() -> None:
    context = baseline_context()
    instruction = buy(quantity=26)

    first = risk.evaluate(instruction, context)
    second = risk.evaluate(instruction, context)

    assert (first.verdict, first.rule_id, first.reason) == (
        second.verdict,
        second.rule_id,
        second.reason,
    )


def test_an_unfinalized_context_is_a_config_error() -> None:
    with pytest.raises(risk.RiskConfigError):
        risk.evaluate(buy(), risk.RiskContext())


def test_a_malformed_universe_is_a_config_error() -> None:
    context = baseline_context()
    context.universe = {"Energy": ["XOM.US"], "Semiconductor": ["XOM.US"]}
    with pytest.raises(risk.RiskConfigError):
        context.finalize()


def test_the_allowlist_gates_entries_not_exits() -> None:
    """BUY out-of-universe is rejected; SELL of the same held symbol is not."""
    context = baseline_context()
    context.positions = {"TSLA.US": risk.Position(quantity=10, avg_cost=90.0)}
    context.last_prices = dict(context.last_prices, **{"TSLA.US": 100.0})

    entry = risk.evaluate(buy(symbol="TSLA.US"), context)
    assert entry.rule_id == risk.rules.UNIVERSE_ALLOWLIST

    exit_ = risk.evaluate(sell(symbol="TSLA.US"), context)
    assert exit_.allowed
    assert context.sector_of("TSLA.US") is None


def test_a_missing_price_gates_entries_not_exits() -> None:
    """BUY unpriced is rejected; SELL of the same held symbol is not."""
    context = baseline_context()
    context.positions = {"NVDA.US": risk.Position(quantity=10, avg_cost=90.0)}
    context.last_prices = {k: v for k, v in context.last_prices.items() if k != "NVDA.US"}

    assert risk.evaluate(buy(), context).rule_id == risk.rules.MISSING_LAST_PRICE
    assert risk.evaluate(sell(), context).allowed


def test_the_universe_is_the_allowlist() -> None:
    context = baseline_context()
    assert context.in_universe("NVDA.US")
    assert not context.in_universe("TSLA.US")
    assert context.sector_of("NVDA.US") == "Semiconductor"
    assert context.sector_of("TSLA.US") is None
    assert len(context.symbol_sector()) == 5


# EvaluateBatch: provisional state across a multi-instruction decision


def test_batch_cannot_jointly_breach_the_cash_reserve() -> None:
    context = baseline_context()
    context.account.cash = 3_000.0

    first = buy(quantity=10, client_order_id="coid-1")
    second = buy(quantity=15, client_order_id="coid-2")

    assert risk.evaluate(first, context).allowed
    assert risk.evaluate(second, context).allowed

    decisions = risk.evaluate_batch([first, second], context)
    assert decisions[0].allowed
    assert not decisions[1].allowed
    assert decisions[1].rule_id == risk.rules.MIN_CASH_RESERVE


def test_batch_cannot_jointly_breach_the_sector_cap() -> None:
    context = baseline_context()
    context.limits.max_position_pct = 0.30
    context.positions = {"AMD.US": risk.Position(quantity=30, avg_cost=50.0)}

    first = buy("NVDA.US", 25, 100.0, "coid-1")
    second = buy("AMD.US", 25, 50.0, "coid-2")

    assert risk.evaluate(first, context).allowed
    assert risk.evaluate(second, context).allowed

    decisions = risk.evaluate_batch([first, second], context)
    assert decisions[0].allowed
    assert not decisions[1].allowed
    assert decisions[1].rule_id == risk.rules.MAX_SECTOR_PCT


def test_batch_catches_a_reused_client_order_id() -> None:
    decisions = risk.evaluate_batch(
        [buy("NVDA.US", 5, 100.0, "coid-dup"), buy("AMD.US", 5, 50.0, "coid-dup")],
        baseline_context(),
    )
    assert decisions[0].allowed
    assert decisions[1].rule_id == risk.rules.DUPLICATE_ORDER


def test_batch_buy_then_sell_consumes_a_day_trade() -> None:
    context = baseline_context()
    context.day_trades_this_week = 2
    context.limits.max_day_trades_per_week = 3

    decisions = risk.evaluate_batch(
        [
            buy("NVDA.US", 10, 100.0, "coid-1"),
            sell("NVDA.US", 10, 100.0, "coid-2"),
            buy("AMD.US", 10, 50.0, "coid-3"),
            sell("AMD.US", 10, 50.0, "coid-4"),
        ],
        context,
    )

    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[3].rule_id == risk.rules.PDT_DAY_TRADE_CAP


def test_batch_does_not_mutate_the_callers_context() -> None:
    context = baseline_context()
    decisions = risk.evaluate_batch(
        [buy("NVDA.US", 10, 100.0, "coid-1"), buy("AMD.US", 10, 50.0, "coid-2")], context
    )

    assert all(d.allowed for d in decisions)
    assert context.account.cash == 10_000.0
    assert context.trades_today == 0
    assert context.positions == {}


def test_empty_batch() -> None:
    assert risk.evaluate_batch([], baseline_context()) == []


def test_apply_allowed_advances_provisional_state() -> None:
    context = baseline_context()
    risk.apply_allowed(buy(quantity=10, client_order_id="coid-1"), context)

    assert context.account.cash == 9_000.0
    assert context.account.buying_power == 9_000.0
    assert context.held_quantity("NVDA.US") == 10
    assert context.trades_today == 1
    assert context.turnover_today == 1_000.0
    assert "coid-1" in context.seen_client_order_ids
    assert "NVDA.US" in context.positions_opened_today


def test_is_day_trade() -> None:
    context = baseline_context()
    context.positions = {"NVDA.US": risk.Position(quantity=10, avg_cost=100.0)}
    context.positions_opened_today = {"NVDA.US"}

    assert risk.is_day_trade(sell(), context)
    assert not risk.is_day_trade(buy(), context)
