"""LongbridgeTradeBroker against a fake AsyncTradeContext. No SDK, no network.

The adapter's I/O is exercised by injecting a fake context: `connect()` is
never called, so nothing imports `longport` or reads a credential. What is
under test is the behaviour that cannot be checked by the live round-trip
without risking real orders -- the two-step fill-id drain, idempotency, and
the rate limiter.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from tevnnis_core.brokers.longbridge import (
    LongbridgeBrokerError,
    LongbridgeTradeBroker,
    _RateLimiter,
)
from tevnnis_core.instructions import Action, OrderType, TradingInstruction

OURS = "co_" + "a1b2c3d4e5f6" * 2
OTHER = "co_" + "f6e5d4c3b2a1" * 2


class FakeStatus:
    def __init__(self, name: str) -> None:
        self._name = name

    def __str__(self) -> str:
        return f"OrderStatus.{self._name}"

    def __hash__(self):
        raise TypeError("unhashable type: 'builtins.OrderStatus'")


@dataclass
class FakePush:
    order_id: str
    status: FakeStatus
    remark: str
    executed_quantity: Decimal = Decimal("0")
    executed_price: Decimal | None = None


@dataclass
class FakeExecution:
    order_id: str
    trade_id: str
    symbol: str
    quantity: Decimal
    price: Decimal
    trade_done_at: int = 0


@dataclass
class FakeChargeDetail:
    total_amount: Decimal
    currency: str = "USD"


@dataclass
class FakeOrderDetail:
    charge_detail: FakeChargeDetail


@dataclass
class FakeOrder:
    order_id: str
    symbol: str
    status: FakeStatus
    remark: str


@dataclass
class FakeSubmitResponse:
    order_id: str


@dataclass
class FakeCtx:
    """Only the methods the adapter actually calls."""

    orders: list[FakeOrder] = field(default_factory=list)
    executions: list[FakeExecution] = field(default_factory=list)
    charge: Decimal = Decimal("0")
    charge_currency: str = "USD"
    submitted: list[dict] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    next_order_id: str = "LB-NEW"

    async def today_orders(self, **kwargs):
        return list(self.orders)

    async def today_executions(self, order_id=None, **kwargs):
        return [e for e in self.executions if order_id is None or e.order_id == order_id]

    async def order_detail(self, order_id):
        return FakeOrderDetail(FakeChargeDetail(self.charge, self.charge_currency))

    async def submit_order(self, **kwargs):
        self.submitted.append(kwargs)
        return FakeSubmitResponse(self.next_order_id)

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)


def broker_with(ctx: FakeCtx) -> LongbridgeTradeBroker:
    broker = LongbridgeTradeBroker()
    broker._ctx = ctx  # connect() bypassed: no network, no credentials
    return broker


def instruction(client_order_id: str = OURS, **kw) -> TradingInstruction:
    return TradingInstruction(
        action=kw.get("action", Action.BUY),
        symbol=kw.get("symbol", "NVDA.US"),
        order_type=OrderType.LIMIT,
        quantity=kw.get("quantity", 10),
        limit_price=kw.get("limit_price", 105.50),
        valid_seconds=0,
        confidence=0.8,
        client_order_id=client_order_id,
    )


# ---------------------------------------------------------------------------
# Construction is inert
# ---------------------------------------------------------------------------


def test_construction_touches_nothing():
    broker = LongbridgeTradeBroker()
    assert not broker.connected


async def test_using_an_unconnected_broker_is_a_clear_error():
    broker = LongbridgeTradeBroker()
    with pytest.raises(LongbridgeBrokerError, match="not connected"):
        await broker.query_account()


# ---------------------------------------------------------------------------
# poll_order_updates — the two-step fill-id drain
# ---------------------------------------------------------------------------


async def test_a_fill_carries_the_brokers_real_trade_id():
    ctx = FakeCtx(
        executions=[FakeExecution("LB-1", "TRADE-77", "NVDA.US", Decimal("10"), Decimal("105.50"))],
        charge=Decimal("0.50"),
    )
    broker = broker_with(ctx)
    broker._on_order_changed(FakePush("LB-1", FakeStatus("Filled"), OURS, Decimal("10")))

    updates = await broker.poll_order_updates()
    assert len(updates) == 1
    update = updates[0]
    assert update.client_order_id == OURS
    assert update.broker_order_id == "LB-1"
    assert update.status == "filled"
    assert update.broker_fill_id == "TRADE-77"  # §7 dedup key, never invented
    assert update.filled_quantity == 10
    assert update.fill_price == 105.50
    assert update.fee == pytest.approx(0.50)


async def test_fill_quantity_is_the_increment_not_the_cumulative_total():
    # PushOrderChanged.executed_quantity is CUMULATIVE; core records per-fill
    # increments. Reading the push figure would double-count the position.
    ctx = FakeCtx(
        executions=[
            FakeExecution("LB-1", "T1", "NVDA.US", Decimal("4"), Decimal("105.00"), 1),
            FakeExecution("LB-1", "T2", "NVDA.US", Decimal("6"), Decimal("105.50"), 2),
        ],
    )
    broker = broker_with(ctx)
    broker._on_order_changed(FakePush("LB-1", FakeStatus("Filled"), OURS, Decimal("10")))

    updates = await broker.poll_order_updates()
    assert [u.filled_quantity for u in updates] == [4, 6]
    assert sum(u.filled_quantity for u in updates) == 10
    # Only the last update carries the terminal status.
    assert [u.status for u in updates] == ["partially_filled", "filled"]
    assert [u.broker_fill_id for u in updates] == ["T1", "T2"]


async def test_a_re_poll_never_re_emits_a_seen_trade_id():
    ctx = FakeCtx(
        executions=[FakeExecution("LB-1", "T1", "NVDA.US", Decimal("10"), Decimal("105.50"))],
    )
    broker = broker_with(ctx)

    broker._on_order_changed(FakePush("LB-1", FakeStatus("Filled"), OURS, Decimal("10")))
    first = await broker.poll_order_updates()
    assert [u.broker_fill_id for u in first] == ["T1"]

    # The broker re-pushes the same order; the execution is already recorded.
    broker._on_order_changed(FakePush("LB-1", FakeStatus("Filled"), OURS, Decimal("10")))
    second = await broker.poll_order_updates()
    assert len(second) == 1
    assert second[0].broker_fill_id is None  # no duplicate fill
    assert second[0].status == "filled"  # but the status still lands


async def test_an_incremental_fill_only_emits_the_new_execution():
    ctx = FakeCtx(
        executions=[FakeExecution("LB-1", "T1", "NVDA.US", Decimal("4"), Decimal("105.00"), 1)],
        charge=Decimal("0.50"),
    )
    broker = broker_with(ctx)
    broker._on_order_changed(FakePush("LB-1", FakeStatus("PartialFilled"), OURS, Decimal("4")))
    first = await broker.poll_order_updates()
    assert [u.broker_fill_id for u in first] == ["T1"]
    assert first[0].fee == pytest.approx(0.50)

    ctx.executions.append(
        FakeExecution("LB-1", "T2", "NVDA.US", Decimal("6"), Decimal("105.50"), 2)
    )
    ctx.charge = Decimal("1.25")  # cumulative
    broker._on_order_changed(FakePush("LB-1", FakeStatus("Filled"), OURS, Decimal("10")))
    second = await broker.poll_order_updates()
    assert [u.broker_fill_id for u in second] == ["T2"]
    assert second[0].fee == pytest.approx(0.75)  # 1.25 cumulative - 0.50 emitted


async def test_a_non_fill_transition_emits_a_bare_status_update():
    broker = broker_with(FakeCtx())
    broker._on_order_changed(FakePush("LB-1", FakeStatus("New"), OURS))
    broker._on_order_changed(FakePush("LB-1", FakeStatus("Canceled"), OURS))

    updates = await broker.poll_order_updates()
    assert [u.status for u in updates] == ["open", "cancelled"]
    assert all(u.broker_fill_id is None for u in updates)
    assert all(u.filled_quantity == 0 for u in updates)


async def test_a_rejected_order_maps_to_rejected():
    broker = broker_with(FakeCtx())
    broker._on_order_changed(FakePush("LB-1", FakeStatus("Rejected"), OURS))
    assert (await broker.poll_order_updates())[0].status == "rejected"


async def test_pushes_for_someone_elses_order_are_ignored():
    # An order placed by hand in the Longbridge app must not fabricate rows.
    broker = broker_with(FakeCtx())
    broker._on_order_changed(FakePush("LB-9", FakeStatus("Filled"), "manual order", Decimal("5")))
    assert await broker.poll_order_updates() == []
    assert any("not placed by this agent" in w for w in broker.warnings)


async def test_a_non_usd_charge_is_not_mixed_into_the_fee():
    ctx = FakeCtx(
        executions=[FakeExecution("LB-1", "T1", "700.HK", Decimal("10"), Decimal("320.00"))],
        charge=Decimal("18.00"),
        charge_currency="HKD",
    )
    broker = broker_with(ctx)
    broker._on_order_changed(FakePush("LB-1", FakeStatus("Filled"), OURS, Decimal("10")))
    updates = await broker.poll_order_updates()
    assert updates[0].fee == 0.0
    assert any("not USD" in w for w in broker.warnings)


async def test_an_unmapped_status_warns_once_and_stays_open():
    broker = broker_with(FakeCtx())
    broker._on_order_changed(FakePush("LB-1", FakeStatus("SomeFutureThing"), OURS))
    broker._on_order_changed(FakePush("LB-1", FakeStatus("SomeFutureThing"), OURS))
    updates = await broker.poll_order_updates()
    assert [u.status for u in updates] == ["open", "open"]
    assert sum("unmapped" in w for w in broker.warnings) == 1


async def test_the_push_buffer_drains_exactly_once():
    broker = broker_with(FakeCtx())
    for _ in range(3):
        broker._on_order_changed(FakePush("LB-1", FakeStatus("New"), OURS))
    assert len(await broker.poll_order_updates()) == 3
    assert await broker.poll_order_updates() == []


# ---------------------------------------------------------------------------
# Idempotency (layer 2) and cancel
# ---------------------------------------------------------------------------


async def test_submit_places_a_day_limit_order_with_our_id_in_remark():
    ctx = FakeCtx(next_order_id="LB-42")
    broker = broker_with(ctx)

    assert await broker.submit_order(instruction()) == "LB-42"
    sent = ctx.submitted[0]
    assert sent["symbol"] == "NVDA.US"  # §3 identity codec
    assert sent["remark"] == OURS
    assert sent["submitted_quantity"] == Decimal(10)
    assert sent["submitted_price"] == Decimal("105.5")
    assert str(sent["order_type"]) == "OrderType.LO"
    assert str(sent["side"]) == "OrderSide.Buy"
    assert str(sent["time_in_force"]) == "TimeInForceType.Day"
    assert str(sent["outside_rth"]) == "OutsideRTH.RTHOnly"


async def test_submit_is_idempotent_against_an_order_already_at_the_broker():
    # The crash window: the order reached Longbridge but core never committed.
    ctx = FakeCtx(orders=[FakeOrder("LB-7", "NVDA.US", FakeStatus("New"), OURS)])
    broker = broker_with(ctx)

    assert await broker.submit_order(instruction()) == "LB-7"
    assert ctx.submitted == [], "must not place a second order"
    assert any("already exists at the broker" in w for w in broker.warnings)


async def test_a_different_client_order_id_still_submits():
    ctx = FakeCtx(orders=[FakeOrder("LB-7", "NVDA.US", FakeStatus("New"), OURS)])
    broker = broker_with(ctx)
    await broker.submit_order(instruction(OTHER))
    assert len(ctx.submitted) == 1


async def test_submit_rejects_what_v0_cannot_express():
    broker = broker_with(FakeCtx())
    with pytest.raises(LongbridgeBrokerError, match="BUY or SELL"):
        await broker.submit_order(instruction(action=Action.HOLD, quantity=0, limit_price=0))


async def test_cancel_resolves_our_id_to_the_broker_order_id():
    ctx = FakeCtx(orders=[FakeOrder("LB-7", "NVDA.US", FakeStatus("New"), OURS)])
    broker = broker_with(ctx)
    await broker.cancel_order(OURS)
    assert ctx.cancelled == ["LB-7"]  # never our client_order_id


async def test_cancelling_an_unknown_order_is_a_clear_error():
    broker = broker_with(FakeCtx())
    with pytest.raises(LongbridgeBrokerError, match="no broker order carries that remark"):
        await broker.cancel_order(OURS)


async def test_open_orders_exclude_terminal_and_foreign_ones():
    ctx = FakeCtx(
        orders=[
            FakeOrder("LB-1", "NVDA.US", FakeStatus("New"), OURS),
            FakeOrder("LB-2", "AMD.US", FakeStatus("Filled"), OTHER),
            FakeOrder("LB-3", "700.HK", FakeStatus("New"), "by hand"),
        ]
    )
    broker = broker_with(ctx)
    open_orders = await broker.query_open_orders()
    assert [o.client_order_id for o in open_orders] == [OURS]
    assert any("not TEVNNIS's" in w for w in broker.warnings)


# ---------------------------------------------------------------------------
# Rate limiter (§3: <=30 calls/30s, >=0.02s apart)
# ---------------------------------------------------------------------------


async def test_rate_limiter_spaces_calls():
    limiter = _RateLimiter(max_calls=30, per_seconds=30.0, min_interval=0.02)
    start = asyncio.get_running_loop().time()
    for _ in range(5):
        await limiter.acquire()
    elapsed = asyncio.get_running_loop().time() - start
    assert elapsed >= 0.02 * 4


async def test_rate_limiter_enforces_the_window():
    limiter = _RateLimiter(max_calls=3, per_seconds=0.5, min_interval=0.0)
    start = asyncio.get_running_loop().time()
    for _ in range(4):  # the 4th must wait for the window to roll
        await limiter.acquire()
    assert asyncio.get_running_loop().time() - start >= 0.4
