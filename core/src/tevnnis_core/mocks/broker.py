"""MockBroker — in-memory TradePort implementation (§13 mock-first).

Simulates a broker's order lifecycle without any network calls: LIMIT orders
fill by a configurable rule (immediate full / partial / no fill), cash and
positions update on fill, fees are computed by a simple rule, and
`query_positions` / `query_open_orders` / `query_account` let core's startup
reconcile (§9.1 step 3) treat this mock exactly like a real broker adapter.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum

from tevnnis_core.instructions import Action, OrderType
from tevnnis_core.ports import AccountSnapshot, OrderSnapshot, OrderUpdate, PositionSnapshot


class FillMode(str, Enum):
    """How MockBroker resolves a newly submitted order."""

    IMMEDIATE_FULL = "immediate_full"
    PARTIAL = "partial"
    NO_FILL = "no_fill"


class MockBrokerError(Exception):
    """Raised when a simulated order cannot be accepted (mirrors a broker reject)."""


@dataclass
class OrderUpdateEvent:
    """One order-lifecycle event, exposed for core (or tests) to consume.

    `broker_fill_id` is the broker's own id for the fill this update carries
    (None when the update carries no fill). §7 makes it the `fills` dedup key,
    so a real broker supplies it and core never invents one — the mock mints a
    deterministic `<broker_order_id>-F<n>`.
    """

    client_order_id: str
    broker_order_id: str
    status: str  # "open" | "partially_filled" | "filled" | "cancelled"
    filled_quantity: int
    fill_price: float | None
    fee: float
    broker_fill_id: str | None = None


@dataclass
class _OpenOrder:
    client_order_id: str
    broker_order_id: str
    symbol: str
    action: Action
    quantity: int  # original quantity
    limit_price: float
    status: str = "open"
    filled_quantity: int = 0
    fill_seq: int = 0


@dataclass
class MockBroker:
    """In-memory fake broker. Not thread-safe; intended for single-loop use."""

    initial_cash: float
    initial_positions: dict[str, PositionSnapshot] = field(default_factory=dict)
    fill_mode: FillMode = FillMode.IMMEDIATE_FULL
    partial_fill_ratio: float = 0.5
    fee_per_share: float = 0.005
    min_fee: float = 0.50

    def __post_init__(self) -> None:
        self._cash = self.initial_cash
        self._positions: dict[str, PositionSnapshot] = dict(self.initial_positions)
        self._orders: dict[str, _OpenOrder] = {}
        self._broker_order_seq = itertools.count(1)
        self.order_events: list[OrderUpdateEvent] = []
        self._drained = 0

    # -- fee rule ------------------------------------------------------------

    def _fee(self, quantity: int) -> float:
        return max(self.min_fee, quantity * self.fee_per_share)

    # -- position/cash bookkeeping --------------------------------------------

    def _apply_fill(
        self, symbol: str, action: Action, quantity: int, price: float, fee: float
    ) -> None:
        notional = quantity * price
        if action == Action.BUY:
            self._cash -= notional + fee
            pos = self._positions.get(symbol)
            if pos is None:
                self._positions[symbol] = PositionSnapshot(
                    symbol=symbol, quantity=quantity, cost_basis=price
                )
            else:
                new_qty = pos.quantity + quantity
                new_cost = (pos.cost_basis * pos.quantity + price * quantity) / new_qty
                pos.quantity = new_qty
                pos.cost_basis = new_cost
        else:  # SELL
            self._cash += notional - fee
            pos = self._positions[symbol]
            pos.quantity -= quantity
            if pos.quantity <= 0:
                del self._positions[symbol]

    # -- TradePort -------------------------------------------------------------

    async def submit_order(self, instruction: object) -> str:
        action = instruction.action  # type: ignore[attr-defined]
        symbol = instruction.symbol  # type: ignore[attr-defined]
        order_type = instruction.order_type  # type: ignore[attr-defined]
        quantity = instruction.quantity  # type: ignore[attr-defined]
        limit_price = instruction.limit_price  # type: ignore[attr-defined]
        client_order_id = instruction.client_order_id  # type: ignore[attr-defined]

        if order_type != OrderType.LIMIT:
            raise MockBrokerError(f"unsupported order_type: {order_type!r}")
        if action not in (Action.BUY, Action.SELL):
            raise MockBrokerError(f"submit_order requires BUY or SELL, got {action!r}")
        if client_order_id in self._orders:
            raise MockBrokerError(f"duplicate client_order_id: {client_order_id}")

        if action == Action.BUY:
            notional = quantity * limit_price + self._fee(quantity)
            if notional > self._cash:
                raise MockBrokerError(
                    f"insufficient buying power: need {notional:.2f}, have {self._cash:.2f}"
                )
        else:
            held = self._positions.get(symbol)
            if held is None or held.quantity < quantity:
                raise MockBrokerError(f"insufficient holdings to sell {quantity} {symbol}")

        broker_order_id = f"MOCK-{next(self._broker_order_seq)}"
        order = _OpenOrder(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            symbol=symbol,
            action=action,
            quantity=quantity,
            limit_price=limit_price,
        )
        self._orders[client_order_id] = order

        if self.fill_mode == FillMode.IMMEDIATE_FULL:
            self._fill(order, quantity)
        elif self.fill_mode == FillMode.PARTIAL:
            partial_qty = max(1, int(quantity * self.partial_fill_ratio))
            partial_qty = min(partial_qty, quantity)
            self._fill(order, partial_qty)
        else:  # NO_FILL
            self.order_events.append(
                OrderUpdateEvent(
                    client_order_id=client_order_id,
                    broker_order_id=broker_order_id,
                    status="open",
                    filled_quantity=0,
                    fill_price=None,
                    fee=0.0,
                )
            )

        return broker_order_id

    def _fill(self, order: _OpenOrder, quantity: int) -> None:
        fee = self._fee(quantity)
        self._apply_fill(order.symbol, order.action, quantity, order.limit_price, fee)
        order.filled_quantity += quantity
        order.fill_seq += 1
        order.status = "filled" if order.filled_quantity >= order.quantity else "partially_filled"
        self.order_events.append(
            OrderUpdateEvent(
                client_order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                status=order.status,
                filled_quantity=quantity,
                fill_price=order.limit_price,
                fee=fee,
                broker_fill_id=f"{order.broker_order_id}-F{order.fill_seq}",
            )
        )

    def simulate_fill(self, client_order_id: str, quantity: int) -> None:
        """Test helper: manually fill (more of) a resting order, e.g. after NO_FILL."""
        order = self._orders.get(client_order_id)
        if order is None:
            raise MockBrokerError(f"unknown client_order_id: {client_order_id}")
        if order.status in ("filled", "cancelled"):
            raise MockBrokerError(f"order {client_order_id} is {order.status}, cannot fill")
        remaining = order.quantity - order.filled_quantity
        if quantity > remaining:
            raise MockBrokerError(f"fill quantity {quantity} exceeds remaining {remaining}")
        self._fill(order, quantity)

    async def cancel_order(self, client_order_id: str) -> None:
        order = self._orders.get(client_order_id)
        if order is None:
            raise MockBrokerError(f"unknown client_order_id: {client_order_id}")
        if order.status in ("filled", "cancelled"):
            raise MockBrokerError(f"order {client_order_id} is already {order.status}")
        order.status = "cancelled"
        self.order_events.append(
            OrderUpdateEvent(
                client_order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                status="cancelled",
                filled_quantity=0,
                fill_price=None,
                fee=0.0,
            )
        )

    async def poll_order_updates(self) -> list[OrderUpdate]:
        """Drain the queued lifecycle events as port-level OrderUpdates.

        `order_events` keeps the full history for test assertions; this returns
        only what has not been drained yet, so repeated polls do not replay a
        fill that core has already persisted.
        """
        pending = self.order_events[self._drained :]
        self._drained = len(self.order_events)
        return [
            OrderUpdate(
                client_order_id=e.client_order_id,
                broker_order_id=e.broker_order_id,
                status=e.status,
                filled_quantity=e.filled_quantity,
                fill_price=e.fill_price,
                fee=e.fee,
                broker_fill_id=e.broker_fill_id,
            )
            for e in pending
        ]

    async def query_positions(self) -> list[PositionSnapshot]:
        return [
            PositionSnapshot(symbol=p.symbol, quantity=p.quantity, cost_basis=p.cost_basis)
            for p in self._positions.values()
        ]

    async def query_open_orders(self) -> list[OrderSnapshot]:
        return [
            OrderSnapshot(
                client_order_id=o.client_order_id,
                status=o.status,
                symbol=o.symbol,
                broker_order_id=o.broker_order_id,
            )
            for o in self._orders.values()
            if o.status in ("open", "partially_filled")
        ]

    async def query_account(self) -> AccountSnapshot:
        holdings_value = sum(p.quantity * p.cost_basis for p in self._positions.values())
        net_liq = self._cash + holdings_value
        return AccountSnapshot(buying_power=self._cash, cash=self._cash, net_liquidation=net_liq)
