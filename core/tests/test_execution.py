"""Execution Engine — the §8 step 8 order lifecycle (submit, TIF, fills, dedup)."""

from __future__ import annotations

from datetime import timedelta

from tevnnis_core.db import repository as repo
from tevnnis_core.db.models import ApiUsage, Fill, Order, Position
from tevnnis_core.execution import DUPLICATE, REJECTED, SKIPPED_HOLD, SUBMITTED, ExecutionEngine
from tevnnis_core.instructions import Action, OrderType, TradingInstruction
from tevnnis_core.mocks.broker import FillMode, MockBroker
from tevnnis_core.ports import OrderSnapshot, OrderUpdate, PositionSnapshot


def instruction(
    *,
    action: Action = Action.BUY,
    symbol: str = "NVDA.US",
    quantity: int = 10,
    limit_price: float = 100.0,
    valid_seconds: int = 300,
    client_order_id: str = "co_1",
) -> TradingInstruction:
    return TradingInstruction(
        action=action,
        symbol=symbol,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price=limit_price,
        valid_seconds=valid_seconds,
        confidence=0.8,
        client_order_id=client_order_id,
    )


def engine(session, broker) -> ExecutionEngine:
    return ExecutionEngine(trade_port=broker, session=session, broker_name="mock")


async def test_submitted_order_fills_and_lands_in_orders_fills_and_positions(
    full_db_session, now
):
    broker = MockBroker(initial_cash=10_000.0)
    execution = engine(full_db_session, broker)

    results = await execution.submit_batch([(instruction(), 1)], now=now)

    assert results[0].outcome == SUBMITTED
    order = repo.get_order(full_db_session, "co_1")
    assert order.status == repo.STATUS_FILLED
    assert order.broker_order_id == "MOCK-1"

    fill = full_db_session.query(Fill).one()
    assert (fill.quantity, fill.price, fill.order_id) == (10, 100.0, order.id)
    assert fill.broker_fill_id == "MOCK-1-F1"  # the broker's id, never invented by core

    position = full_db_session.query(Position).one()
    assert (position.symbol, position.quantity, position.cost_basis) == ("NVDA.US", 10, 100.0)


async def test_every_submission_writes_a_broker_row_to_the_api_usage_ledger(full_db_session, now):
    broker = MockBroker(initial_cash=10_000.0)
    await engine(full_db_session, broker).submit_batch([(instruction(), 1)], now=now)

    usage = full_db_session.query(ApiUsage).filter(ApiUsage.kind == "broker").one()
    assert usage.provider == "mock"
    assert usage.call_count == 1


async def test_hold_places_no_order(full_db_session, now):
    broker = MockBroker(initial_cash=10_000.0)
    hold = TradingInstruction(
        action=Action.HOLD,
        symbol="NVDA.US",
        quantity=0,
        limit_price=0.0,
        valid_seconds=0,
        confidence=0.5,
        client_order_id="co_hold",
    )
    result = await engine(full_db_session, broker).submit(hold, 1, now=now)

    assert result.outcome == SKIPPED_HOLD
    assert full_db_session.query(Order).count() == 0


async def test_resubmitting_the_same_client_order_id_is_a_no_op(full_db_session, now):
    """§9.3 — a retry of the same persisted decision cannot double-order."""
    broker = MockBroker(initial_cash=10_000.0)
    execution = engine(full_db_session, broker)

    await execution.submit(instruction(), 1, now=now)
    second = await execution.submit(instruction(), 1, now=now)

    assert second.outcome == DUPLICATE
    assert full_db_session.query(Order).count() == 1
    assert len(await broker.query_positions()) == 1


async def test_a_broker_reject_is_recorded_and_does_not_raise(full_db_session, now):
    broker = MockBroker(initial_cash=50.0)  # nowhere near enough for 10 x 100
    result = await engine(full_db_session, broker).submit(instruction(), 1, now=now)

    assert result.outcome == REJECTED
    assert "insufficient buying power" in result.message
    assert repo.get_order(full_db_session, "co_1").status == repo.STATUS_REJECTED
    assert full_db_session.query(Fill).count() == 0


async def test_replayed_order_update_does_not_double_count_a_fill(full_db_session, now):
    """Fills dedup on the broker's fill id (§7), so a replayed push is harmless."""
    broker = MockBroker(initial_cash=10_000.0)
    execution = engine(full_db_session, broker)
    await execution.submit_batch([(instruction(), 1)], now=now)

    replay = OrderUpdate(
        client_order_id="co_1",
        broker_order_id="MOCK-1",
        status="filled",
        filled_quantity=10,
        fill_price=100.0,
        fee=0.5,
        broker_fill_id="MOCK-1-F1",
    )
    execution._apply_update(replay, now=now)

    assert full_db_session.query(Fill).count() == 1
    assert full_db_session.query(Position).one().quantity == 10


async def test_tif_expiry_cancels_the_unfilled_remainder_after_a_partial_fill(
    full_db_session, now
):
    broker = MockBroker(initial_cash=10_000.0, fill_mode=FillMode.PARTIAL)
    execution = engine(full_db_session, broker)
    await execution.submit_batch([(instruction(valid_seconds=300), 1)], now=now)

    assert repo.get_order(full_db_session, "co_1").status == repo.STATUS_PARTIALLY_FILLED
    assert full_db_session.query(Fill).one().quantity == 5

    # Still inside the TIF window: nothing is cancelled.
    assert await execution.expire_timed_orders(now=now + timedelta(seconds=299)) == []

    expired = await execution.expire_timed_orders(now=now + timedelta(seconds=300))

    assert expired == ["co_1"]
    assert repo.get_order(full_db_session, "co_1").status == repo.STATUS_CANCELLED
    # The partial fill stands; only the remainder was cancelled.
    assert full_db_session.query(Fill).count() == 1
    assert full_db_session.query(Position).one().quantity == 5


async def test_a_fully_filled_order_is_never_cancelled_by_the_tif(full_db_session, now):
    broker = MockBroker(initial_cash=10_000.0)
    execution = engine(full_db_session, broker)
    await execution.submit_batch([(instruction(valid_seconds=60), 1)], now=now)

    assert await execution.expire_timed_orders(now=now + timedelta(hours=1)) == []
    assert repo.get_order(full_db_session, "co_1").status == repo.STATUS_FILLED


async def test_valid_seconds_zero_means_no_tif_deadline(full_db_session, now):
    broker = MockBroker(initial_cash=10_000.0, fill_mode=FillMode.NO_FILL)
    execution = engine(full_db_session, broker)
    await execution.submit_batch([(instruction(valid_seconds=0), 1)], now=now)

    assert await execution.expire_timed_orders(now=now + timedelta(days=1)) == []
    assert repo.get_order(full_db_session, "co_1").status == repo.STATUS_OPEN


async def test_shutdown_cancels_everything_still_working(full_db_session, now):
    broker = MockBroker(initial_cash=10_000.0, fill_mode=FillMode.NO_FILL)
    execution = engine(full_db_session, broker)
    await execution.submit_batch(
        [
            (instruction(client_order_id="co_1"), 1),
            (instruction(client_order_id="co_2", symbol="AMD.US", limit_price=50.0), 2),
        ],
        now=now,
    )

    cancelled = await execution.cancel_all_inflight(now=now)

    assert sorted(cancelled) == ["co_1", "co_2"]
    assert execution.inflight_ids == []
    assert repo.get_order(full_db_session, "co_1").status == repo.STATUS_CANCELLED
    assert repo.get_order(full_db_session, "co_2").status == repo.STATUS_CANCELLED
    assert await broker.query_open_orders() == []


async def test_a_later_fill_of_a_resting_order_is_persisted_on_the_next_poll(
    full_db_session, now
):
    broker = MockBroker(initial_cash=10_000.0, fill_mode=FillMode.NO_FILL)
    execution = engine(full_db_session, broker)
    await execution.submit_batch([(instruction(), 1)], now=now)
    assert full_db_session.query(Fill).count() == 0

    broker.simulate_fill("co_1", 10)
    await execution.poll_updates(now=now + timedelta(seconds=30))

    assert full_db_session.query(Fill).one().quantity == 10
    assert repo.get_order(full_db_session, "co_1").status == repo.STATUS_FILLED
    assert full_db_session.query(Position).one().quantity == 10


async def test_a_sell_fill_reduces_the_cached_position(full_db_session, now):
    broker = MockBroker(
        initial_cash=10_000.0,
        initial_positions={
            "AMD.US": PositionSnapshot(symbol="AMD.US", quantity=20, cost_basis=48.0)
        },
    )
    repo.apply_fill_to_position(
        full_db_session, symbol="AMD.US", action=Action.BUY, quantity=20, price=48.0
    )
    execution = engine(full_db_session, broker)

    await execution.submit_batch(
        [(instruction(action=Action.SELL, symbol="AMD.US", quantity=5, limit_price=50.0), 1)],
        now=now,
    )

    assert full_db_session.query(Position).one().quantity == 15


async def test_adopted_broker_order_is_tracked_but_never_tif_cancelled(full_db_session, now):
    """§9.1 step 3 — we do not know a previous run's TIF, so we never guess it."""
    broker = MockBroker(initial_cash=10_000.0, fill_mode=FillMode.NO_FILL)
    execution = engine(full_db_session, broker)
    await execution.submit(instruction(client_order_id="co_prev"), 1, now=now)

    fresh = ExecutionEngine(trade_port=broker, session=full_db_session, broker_name="mock")
    fresh.adopt(
        OrderSnapshot(
            client_order_id="co_prev",
            status=repo.STATUS_OPEN,
            symbol="NVDA.US",
            broker_order_id="MOCK-1",
        ),
        now=now,
    )

    assert fresh.inflight_ids == ["co_prev"]
    assert await fresh.expire_timed_orders(now=now + timedelta(days=1)) == []
    assert await fresh.cancel_all_inflight(now=now) == ["co_prev"]
