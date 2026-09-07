"""LongbridgeTradeBroker — the real TradePort against a Longbridge PAPER account.

Implements all six TradePort methods against `AsyncTradeContext`
(natively async, so no thread-pool wrapper). Every payload mapping lives in
`mapping.py` and is pure and offline-tested; this module owns the network,
the push buffer and the rate limiter, and nothing else.

THERE IS NO PAPER/LIVE SWITCH HERE, BY DESIGN. A flag that can select an
account is a flag that can select the wrong one. The account is whatever
LONGPORT_APP_KEY / LONGPORT_APP_SECRET / LONGPORT_ACCESS_TOKEN map to; the
operator verifies it with scripts/longbridge_account_smoke.py before enabling
this adapter, and cli.py refuses `--broker longbridge` unless the config says
`account.mode: paper` (a fail-closed refusal that routes nothing).

CONSTRUCTION MAKES NO NETWORK CALL. `__init__` only allocates; `connect()`
does the I/O. That keeps `--yes`/`--now` guard tests able to construct the real
class with no credentials, and it means an unconnected adapter can never
accidentally reach the broker.

Secrets (§12): the SDK reads the three env vars itself inside
`Config.from_apikey_env()`. This module never reads, copies, stores or logs a
credential value, and every SDK-sourced string is passed through
`redact_secrets` before it reaches a log or an exception message.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from decimal import Decimal
from typing import Any

from tevnnis_core.brokers.mapping import (
    STATUS_PARTIALLY_FILLED,
    BrokerMappingError,
    FeeLedger,
    client_order_id_from_remark,
    is_unknown_status,
    map_account,
    map_open_orders,
    map_order_status,
    map_positions,
    redact_secrets,
    split_fee,
)
from tevnnis_core.instructions import Action, OrderType
from tevnnis_core.ports import AccountSnapshot, OrderSnapshot, OrderUpdate, PositionSnapshot

_LIVE_STATUSES = ("open", "partially_filled")


class LongbridgeBrokerError(RuntimeError):
    """A broker call failed. The message is already redacted (§12)."""


class _RateLimiter:
    """§3 trade limits: <= 30 calls / 30s, and >= 0.02s between calls.

    Every SDK call goes through this, because one `poll_order_updates` can fan
    out to `today_executions` plus an `order_detail` per fill-bearing event.
    """

    def __init__(
        self, max_calls: int = 30, per_seconds: float = 30.0, min_interval: float = 0.02
    ) -> None:
        self._max_calls = max_calls
        self._per_seconds = per_seconds
        self._min_interval = min_interval
        self._calls: deque[float] = deque()
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self._per_seconds:
                    self._calls.popleft()

                spacing_wait = max(0.0, self._last + self._min_interval - now)
                window_wait = 0.0
                if len(self._calls) >= self._max_calls:
                    window_wait = self._per_seconds - (now - self._calls[0])

                wait = max(spacing_wait, window_wait)
                if wait <= 0:
                    break
                await asyncio.sleep(wait)

            now = time.monotonic()
            self._calls.append(now)
            self._last = now


class LongbridgeTradeBroker:
    """TradePort backed by a real Longbridge paper account."""

    def __init__(self, *, call_timeout: float = 20.0) -> None:
        # Allocation only — no SDK import, no network, no credentials read.
        self._ctx: Any = None
        self._call_timeout = call_timeout
        self._limiter = _RateLimiter()
        # Buffer for pushed order-changed events. A plain deque is the correct
        # primitive here, verified rather than assumed: the SDK invokes the
        # callback SYNCHRONOUSLY on its own Rust push thread while holding the
        # GIL (python/src/trade/push.rs does
        # `Python::attach(|py| callback.bind(py).call(...))`), and only
        # schedules onto an event loop if the callback RETURNS a coroutine.
        # Ours is sync and returns None, so nothing crosses to the loop.
        # deque.append/popleft are atomic under the GIL, so this needs no lock;
        # an asyncio.Queue would have been wrong (not thread-safe, and awaiting
        # from a foreign thread is unsound). The pull-shaped TradePort makes
        # this natural: poll_order_updates drains, it never awaits an item.
        self._pushes: deque[Any] = deque()
        self._seen_trade_ids: set[str] = set()
        self._fees = FeeLedger()
        # client_order_id -> broker order id, rebuilt from today_orders().
        self._remark_index: dict[str, str] = {}
        self._foreign_open_orders = 0
        self._warned_unknown_status: set[str] = set()
        self._warned_foreign_push = False
        self.warnings: list[str] = []

    # -- lifecycle -----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._ctx is not None

    async def connect(self) -> AccountSnapshot:
        """§9.1 step 2 — connect and validate before anything trades.

        Returns the mapped account so the caller can print it. Raises
        LongbridgeBrokerError on any failure, aborting startup before the
        process claims readiness.
        """
        from longport.openapi import AsyncTradeContext, Config, TopicType

        missing = [
            name
            for name in ("LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_ACCESS_TOKEN")
            if not _env(name)
        ]
        if missing:
            # Names only, never values (§12).
            raise LongbridgeBrokerError(
                "missing credential environment variable(s): " + ", ".join(missing)
            )

        try:
            config = Config.from_apikey_env()
        except Exception as exc:  # noqa: BLE001
            raise LongbridgeBrokerError(
                "could not build a Longbridge config from the environment: "
                + redact_secrets(str(exc))
            ) from exc

        # The loop is passed because the SDK uses it only to schedule a
        # coroutine RETURNED by a callback; ours is deliberately sync, so it
        # currently goes unused. Passing it keeps the contract explicit.
        self._ctx = AsyncTradeContext.create(config, asyncio.get_running_loop())

        account = await self.query_account()

        self._ctx.set_on_order_changed(self._on_order_changed)
        await self._call(self._ctx.subscribe([TopicType.Private]), "subscribe")
        await self._refresh_remark_index()
        return account

    async def close(self) -> None:
        if self._ctx is None:
            return
        try:
            from longport.openapi import TopicType

            await self._call(self._ctx.unsubscribe([TopicType.Private]), "unsubscribe")
        except Exception as exc:  # noqa: BLE001 — teardown must not raise
            self._warn(f"unsubscribe failed during shutdown: {redact_secrets(str(exc))}")
        finally:
            self._ctx = None

    # -- plumbing ------------------------------------------------------------

    def _require_ctx(self) -> Any:
        if self._ctx is None:
            raise LongbridgeBrokerError(
                "broker is not connected — call connect() before using it"
            )
        return self._ctx

    async def _call(self, awaitable: Any, what: str) -> Any:
        """Rate-limit, await with a timeout, and redact any error."""
        await self._limiter.acquire()
        try:
            return await asyncio.wait_for(awaitable, timeout=self._call_timeout)
        except asyncio.TimeoutError as exc:
            raise LongbridgeBrokerError(
                f"{what} timed out after {self._call_timeout:g}s"
            ) from exc
        except LongbridgeBrokerError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LongbridgeBrokerError(
                f"{what} failed: {type(exc).__name__}: {redact_secrets(str(exc))}"
            ) from exc

    def _warn(self, message: str) -> None:
        self.warnings.append(message)

    def _on_order_changed(self, event: Any) -> None:
        """SDK push callback. Runs on the SDK's Rust push thread — see __init__.

        Does the absolute minimum and never raises: an exception here would
        escape into Rust, and any real work would hold the GIL on a foreign
        thread. All interpretation happens later, on the event loop, in
        poll_order_updates.
        """
        try:
            self._pushes.append(event)
        except Exception:  # noqa: BLE001 — must never propagate into the SDK
            pass

    async def _refresh_remark_index(self) -> None:
        """Rebuild client_order_id -> broker order id from today's orders.

        Day-scoped is the right scope: §3 makes orders day-scoped and we always
        submit TimeInForceType.Day.
        """
        ctx = self._require_ctx()
        orders = await self._call(ctx.today_orders(), "today_orders")
        index: dict[str, str] = {}
        for order in orders:
            client_order_id = client_order_id_from_remark(getattr(order, "remark", None))
            if client_order_id is not None:
                index[client_order_id] = order.order_id
        self._remark_index = index

    # -- TradePort -----------------------------------------------------------

    async def submit_order(self, instruction: Any) -> str:
        """Submit one LIMIT order; return the broker order id.

        IDEMPOTENCY (§7, §9.3) has three layers, because Longbridge does NOT
        enforce uniqueness on `remark` — passing our client_order_id through it
        is identification, not a broker-side guarantee:

          1. core refuses a duplicate before we are ever called
             (execution.submit checks repo.get_order, backed by the
             orders.client_order_id UNIQUE constraint);
          2. HERE: the crash window where an order reached the broker but the
             DB commit did not. today_orders() is re-read and, if our
             client_order_id already appears as a remark, the EXISTING broker
             order id is returned instead of placing a second order;
          3. reconcile-by-remark on startup re-adopts our resting orders
             (query_open_orders), so a restart converges without double-placing.
        """
        from longport.openapi import OrderSide, OutsideRTH, TimeInForceType
        from longport.openapi import OrderType as LbOrderType

        ctx = self._require_ctx()

        if instruction.order_type != OrderType.LIMIT:
            raise LongbridgeBrokerError(f"unsupported order_type: {instruction.order_type!r}")
        if instruction.action not in (Action.BUY, Action.SELL):
            raise LongbridgeBrokerError(
                f"submit_order requires BUY or SELL, got {instruction.action!r}"
            )
        if instruction.quantity <= 0:
            raise LongbridgeBrokerError(f"quantity must be > 0, got {instruction.quantity}")
        if not (instruction.limit_price > 0):
            raise LongbridgeBrokerError(f"limit_price must be > 0, got {instruction.limit_price}")

        client_order_id = instruction.client_order_id

        # Layer 2: never place a second order for an id the broker already has.
        await self._refresh_remark_index()
        existing = self._remark_index.get(client_order_id)
        if existing is not None:
            self._warn(
                f"order {client_order_id} already exists at the broker as {existing}; "
                "returning it instead of submitting again"
            )
            return existing

        response = await self._call(
            ctx.submit_order(
                symbol=instruction.symbol,  # §3: identity codec in v0
                order_type=LbOrderType.LO,  # our only OrderType is LIMIT
                side=OrderSide.Buy if instruction.action == Action.BUY else OrderSide.Sell,
                submitted_quantity=Decimal(int(instruction.quantity)),
                # Both our TIF.DAY and TIF.TIMED submit as Day: §3 says orders
                # are day-scoped and we implement TIMED ourselves on top, via
                # ExecutionEngine.expire_timed_orders cancelling the remainder.
                time_in_force=TimeInForceType.Day,
                submitted_price=Decimal(str(instruction.limit_price)),
                # v0 trades the regular session only (§3).
                outside_rth=OutsideRTH.RTHOnly,
                remark=client_order_id,
            ),
            "submit_order",
        )
        self._remark_index[client_order_id] = response.order_id
        return response.order_id

    async def cancel_order(self, client_order_id: str) -> None:
        """Cancel by our idempotency key.

        The SDK cancels by BROKER order id, so this resolves through the
        remark index, re-reading today's orders on a miss.
        """
        ctx = self._require_ctx()
        broker_order_id = self._remark_index.get(client_order_id)
        if broker_order_id is None:
            await self._refresh_remark_index()
            broker_order_id = self._remark_index.get(client_order_id)
        if broker_order_id is None:
            raise LongbridgeBrokerError(
                f"cannot cancel {client_order_id}: no broker order carries that remark today"
            )
        await self._call(ctx.cancel_order(broker_order_id), "cancel_order")

    async def query_account(self) -> AccountSnapshot:
        """USD only, from cash_infos — see mapping.map_account for the why."""
        ctx = self._require_ctx()
        # Deliberately unfiltered: account_balance(currency="USD") returns the
        # whole-account CONVERTED aggregate, not USD holdings (mapping.py).
        balances = await self._call(ctx.account_balance(), "account_balance")
        try:
            return map_account(balances)
        except BrokerMappingError as exc:
            raise LongbridgeBrokerError(str(exc)) from exc

    async def query_positions(self) -> list[PositionSnapshot]:
        ctx = self._require_ctx()
        response = await self._call(ctx.stock_positions(), "stock_positions")
        try:
            return map_positions(getattr(response, "channels", None) or [])
        except BrokerMappingError as exc:
            raise LongbridgeBrokerError(str(exc)) from exc

    async def query_open_orders(self) -> list[OrderSnapshot]:
        ctx = self._require_ctx()
        orders = await self._call(ctx.today_orders(), "today_orders")
        mapped = map_open_orders(orders)
        live = [o for o in mapped.ours if o.status in _LIVE_STATUSES]
        self._remark_index.update(
            {o.client_order_id: o.broker_order_id for o in mapped.ours if o.broker_order_id}
        )
        self._foreign_open_orders = mapped.foreign
        if mapped.foreign:
            self._warn(
                f"{mapped.foreign} order(s) at the broker are not TEVNNIS's "
                "(no recognisable client_order_id in remark) — not adopted"
            )
        return live

    async def poll_order_updates(self) -> list[OrderUpdate]:
        """Drain pushed order changes into OrderUpdates (§8 step 8).

        Two-step, because the push carries NO fill id: `PushOrderChanged` has
        only a CUMULATIVE `executed_quantity` and an average `executed_price`.
        The broker's real fill id lives on `Execution.trade_id`, so any event
        that reports execution is followed by `today_executions(order_id=...)`
        and one OrderUpdate is emitted per previously-unseen trade_id. §7 makes
        broker_fill_id the fills dedup key, so core must never invent one.

        Quantities are per-execution INCREMENTS, matching what
        ExecutionEngine._apply_update records (and MockBroker's semantics) --
        never the cumulative executed_quantity.
        """
        events: list[Any] = []
        while True:
            try:
                events.append(self._pushes.popleft())
            except IndexError:
                break

        updates: list[OrderUpdate] = []
        for event in events:
            client_order_id = client_order_id_from_remark(getattr(event, "remark", None))
            if client_order_id is None:
                # Someone else's order (e.g. placed by hand in the app). Not
                # ours to record; recording it would fabricate an orders row.
                if not self._warned_foreign_push:
                    self._warned_foreign_push = True
                    self._warn(
                        "ignoring order-change pushes for orders without a TEVNNIS "
                        "client_order_id in remark (not placed by this agent)"
                    )
                continue

            if is_unknown_status(event.status) and str(event.status) not in (
                self._warned_unknown_status
            ):
                self._warned_unknown_status.add(str(event.status))
                self._warn(
                    f"unmapped Longbridge order status {str(event.status)!r} — "
                    "treated as open"
                )

            status = map_order_status(event.status)
            self._remark_index.setdefault(client_order_id, event.order_id)
            updates.extend(await self._updates_for(event, client_order_id, status))
        return updates

    async def _updates_for(
        self, event: Any, client_order_id: str, status: str
    ) -> list[OrderUpdate]:
        executed = _decimal(getattr(event, "executed_quantity", 0))
        if executed <= 0:
            return [
                OrderUpdate(
                    client_order_id=client_order_id,
                    broker_order_id=event.order_id,
                    status=status,
                )
            ]

        ctx = self._require_ctx()
        executions = await self._call(
            ctx.today_executions(order_id=event.order_id), "today_executions"
        )
        fresh = [e for e in executions if e.trade_id not in self._seen_trade_ids]
        if not fresh:
            # Every execution is already recorded; still emit the lifecycle
            # transition so an order cannot be stranded in a stale status.
            return [
                OrderUpdate(
                    client_order_id=client_order_id,
                    broker_order_id=event.order_id,
                    status=status,
                )
            ]

        fresh.sort(key=lambda e: getattr(e, "trade_done_at", None) or 0)
        quantities = [int(_decimal(e.quantity)) for e in fresh]
        fees = split_fee(await self._incremental_fee(event.order_id), quantities)

        updates: list[OrderUpdate] = []
        for index, execution in enumerate(fresh):
            self._seen_trade_ids.add(execution.trade_id)
            last = index == len(fresh) - 1
            updates.append(
                OrderUpdate(
                    client_order_id=client_order_id,
                    broker_order_id=event.order_id,
                    # Only the final update carries the event's terminal status;
                    # earlier executions are, by definition, partial.
                    status=status if last else STATUS_PARTIALLY_FILLED,
                    filled_quantity=quantities[index],
                    fill_price=float(_decimal(execution.price)),
                    fee=fees[index],
                    broker_fill_id=execution.trade_id,
                )
            )
        return updates

    async def _incremental_fee(self, broker_order_id: str) -> float:
        """Fees charged for this order since the last poll.

        Longbridge reports charges cumulatively per order
        (OrderDetail.charge_detail), while core records a fee per fill.
        """
        ctx = self._require_ctx()
        try:
            detail = await self._call(
                ctx.order_detail(broker_order_id), "order_detail"
            )
        except LongbridgeBrokerError as exc:
            self._warn(f"could not read fees for order {broker_order_id}: {exc}")
            return 0.0

        charge = getattr(detail, "charge_detail", None)
        if charge is None:
            return 0.0
        currency = str(getattr(charge, "currency", "") or "").upper()
        if currency and currency != "USD":
            # Never mix currencies into a USD-denominated fee.
            self._warn(
                f"order {broker_order_id} charged in {currency}, not USD — "
                "recording fee 0.00 rather than mixing currencies"
            )
            return 0.0
        return self._fees.take(broker_order_id, float(_decimal(charge.total_amount)))


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0))
    except Exception:  # noqa: BLE001
        return Decimal(0)


def _env(name: str) -> str | None:
    """Presence check only — the value is never read past emptiness (§12)."""
    import os

    return os.environ.get(name)
