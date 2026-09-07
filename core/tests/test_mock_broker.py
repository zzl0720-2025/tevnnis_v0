"""Tests for MockBroker: fills (immediate/partial/no-fill), cancel, reconciliation."""

from __future__ import annotations

import pytest

from tevnnis_core.instructions import Action, OrderType, TradingInstruction
from tevnnis_core.mocks.broker import FillMode, MockBroker, MockBrokerError
from tevnnis_core.ports import TradePort


def _buy(symbol="AAPL.US", quantity=10, limit_price=100.0, client_order_id="co-1"):
    return TradingInstruction(
        action=Action.BUY,
        symbol=symbol,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price=limit_price,
        valid_seconds=300,
        confidence=0.8,
        client_order_id=client_order_id,
    )


def _sell(symbol="AAPL.US", quantity=10, limit_price=100.0, client_order_id="co-1"):
    return TradingInstruction(
        action=Action.SELL,
        symbol=symbol,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price=limit_price,
        valid_seconds=300,
        confidence=0.8,
        client_order_id=client_order_id,
    )


def test_satisfies_trade_port_protocol():
    broker = MockBroker(initial_cash=10_000.0)
    assert isinstance(broker, TradePort)


@pytest.mark.asyncio
async def test_immediate_full_fill_updates_cash_and_position():
    broker = MockBroker(initial_cash=10_000.0, fill_mode=FillMode.IMMEDIATE_FULL)

    broker_order_id = await broker.submit_order(_buy(quantity=10, limit_price=100.0))
    assert broker_order_id

    positions = await broker.query_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL.US"
    assert positions[0].quantity == 10
    assert positions[0].cost_basis == 100.0

    account = await broker.query_account()
    fee = max(0.50, 10 * 0.005)
    assert account.cash == pytest.approx(10_000.0 - 10 * 100.0 - fee)

    open_orders = await broker.query_open_orders()
    assert open_orders == []

    assert len(broker.order_events) == 1
    assert broker.order_events[0].status == "filled"
    assert broker.order_events[0].filled_quantity == 10


@pytest.mark.asyncio
async def test_partial_fill_leaves_order_open_with_remaining_quantity():
    broker = MockBroker(initial_cash=10_000.0, fill_mode=FillMode.PARTIAL, partial_fill_ratio=0.5)

    await broker.submit_order(_buy(quantity=10, limit_price=100.0))

    open_orders = await broker.query_open_orders()
    assert len(open_orders) == 1
    assert open_orders[0].status == "partially_filled"

    positions = await broker.query_positions()
    assert positions[0].quantity == 5

    assert broker.order_events[-1].status == "partially_filled"
    assert broker.order_events[-1].filled_quantity == 5


@pytest.mark.asyncio
async def test_no_fill_order_stays_open_until_manually_filled():
    broker = MockBroker(initial_cash=10_000.0, fill_mode=FillMode.NO_FILL)

    await broker.submit_order(_buy(quantity=10, limit_price=100.0))

    open_orders = await broker.query_open_orders()
    assert len(open_orders) == 1
    assert open_orders[0].status == "open"
    assert (await broker.query_positions()) == []

    broker.simulate_fill("co-1", 10)

    open_orders = await broker.query_open_orders()
    assert open_orders == []
    positions = await broker.query_positions()
    assert positions[0].quantity == 10


@pytest.mark.asyncio
async def test_cancel_open_order_removes_it_from_open_orders():
    broker = MockBroker(initial_cash=10_000.0, fill_mode=FillMode.NO_FILL)
    await broker.submit_order(_buy(quantity=10, limit_price=100.0))

    await broker.cancel_order("co-1")

    open_orders = await broker.query_open_orders()
    assert open_orders == []
    assert broker.order_events[-1].status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_partial_fill_keeps_the_filled_portion():
    broker = MockBroker(initial_cash=10_000.0, fill_mode=FillMode.PARTIAL, partial_fill_ratio=0.5)
    await broker.submit_order(_buy(quantity=10, limit_price=100.0))

    await broker.cancel_order("co-1")

    positions = await broker.query_positions()
    assert positions[0].quantity == 5
    assert (await broker.query_open_orders()) == []


@pytest.mark.asyncio
async def test_cancel_already_filled_order_raises():
    broker = MockBroker(initial_cash=10_000.0, fill_mode=FillMode.IMMEDIATE_FULL)
    await broker.submit_order(_buy(quantity=10, limit_price=100.0))

    with pytest.raises(MockBrokerError):
        await broker.cancel_order("co-1")


@pytest.mark.asyncio
async def test_insufficient_cash_rejects_buy():
    broker = MockBroker(initial_cash=100.0)
    with pytest.raises(MockBrokerError):
        await broker.submit_order(_buy(quantity=10, limit_price=100.0))


@pytest.mark.asyncio
async def test_insufficient_holdings_rejects_sell():
    broker = MockBroker(initial_cash=10_000.0)
    with pytest.raises(MockBrokerError):
        await broker.submit_order(_sell(quantity=10, limit_price=100.0))


@pytest.mark.asyncio
async def test_sell_reduces_existing_position():
    broker = MockBroker(initial_cash=10_000.0, fill_mode=FillMode.IMMEDIATE_FULL)
    await broker.submit_order(_buy(quantity=10, limit_price=100.0, client_order_id="co-buy"))
    await broker.submit_order(_sell(quantity=4, limit_price=110.0, client_order_id="co-sell"))

    positions = await broker.query_positions()
    assert positions[0].quantity == 6

    account = await broker.query_account()
    fee_buy = max(0.50, 10 * 0.005)
    fee_sell = max(0.50, 4 * 0.005)
    expected_cash = 10_000.0 - (10 * 100.0 + fee_buy) + (4 * 110.0 - fee_sell)
    assert account.cash == pytest.approx(expected_cash)


@pytest.mark.asyncio
async def test_duplicate_client_order_id_rejected():
    broker = MockBroker(initial_cash=10_000.0)
    await broker.submit_order(_buy(client_order_id="co-1"))
    with pytest.raises(MockBrokerError):
        await broker.submit_order(_buy(client_order_id="co-1"))


@pytest.mark.asyncio
async def test_reconciliation_reflects_seeded_state():
    from tevnnis_core.ports import PositionSnapshot

    seed_position = PositionSnapshot(symbol="MSFT.US", quantity=3, cost_basis=300.0)
    broker = MockBroker(initial_cash=5_000.0, initial_positions={"MSFT.US": seed_position})

    positions = await broker.query_positions()
    account = await broker.query_account()
    open_orders = await broker.query_open_orders()

    assert positions == [PositionSnapshot(symbol="MSFT.US", quantity=3, cost_basis=300.0)]
    assert account.cash == 5_000.0
    assert account.net_liquidation == pytest.approx(5_000.0 + 3 * 300.0)
    assert open_orders == []
