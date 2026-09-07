"""Execution Engine — the §8 step 8 order lifecycle over the TradePort.

Submit a LIMIT order, track its updates, enforce **our** TIF by cancelling the
unfilled remainder on timeout, persist fills, move the position cache. Nothing
here decides anything: by the time an instruction arrives it has already been
validated (§5) and allowed by Risk (§11).

Idempotency is the spine of the whole thing (§9.3). `client_order_id` is a
deterministic function of the persisted decision identity, `orders` is unique
on it, and `submit()` checks for an existing row first — so a crash and retry
of the same decision cannot double-order. Fills dedup on the broker's own fill
id for the same reason.

Every method takes `now` explicitly; the engine never reads the wall clock, so
TIF expiry is exactly testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from tevnnis_core.db import repository as repo
from tevnnis_core.db.models import Instruction
from tevnnis_core.instructions import Action, TradingInstruction
from tevnnis_core.ports import OrderSnapshot, OrderUpdate, TradePort

SUBMITTED = "submitted"
DUPLICATE = "duplicate"
REJECTED = "rejected"
SKIPPED_HOLD = "skipped_hold"


@dataclass
class SubmitResult:
    client_order_id: str
    outcome: str  # SUBMITTED | DUPLICATE | REJECTED | SKIPPED_HOLD
    broker_order_id: str | None = None
    message: str = ""


@dataclass
class _Inflight:
    """An order we believe is still working, with its TIF deadline."""

    client_order_id: str
    order_row_id: int
    symbol: str
    action: Action
    quantity: int
    limit_price: float
    deadline: datetime | None  # None => adopted from the broker; TIF unknown, no auto-cancel
    filled_quantity: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.quantity - self.filled_quantity)


@dataclass
class ExecutionEngine:
    """Owns order state for one run; the caller owns the DB transaction."""

    trade_port: TradePort
    session: Session
    broker_name: str = "mock"

    def __post_init__(self) -> None:
        self._inflight: dict[str, _Inflight] = {}
        self.submitted_count = 0
        self.rejected_count = 0
        self.filled_count = 0
        self.cancelled_count = 0

    @property
    def inflight_ids(self) -> list[str]:
        return list(self._inflight)

    # -- submission ----------------------------------------------------------

    async def submit(
        self, instruction: TradingInstruction, instruction_id: int, *, now: datetime
    ) -> SubmitResult:
        """Submit one allowed instruction. A broker reject is recorded, never raised."""
        client_order_id = instruction.client_order_id

        if instruction.action == Action.HOLD:
            # Risk allows HOLD as a no-op (rule HOLD_NO_OP); there is no order.
            return SubmitResult(client_order_id, SKIPPED_HOLD)

        if repo.get_order(self.session, client_order_id) is not None:
            return SubmitResult(
                client_order_id, DUPLICATE, message="client_order_id already submitted"
            )

        order = repo.record_order(
            self.session,
            client_order_id=client_order_id,
            status=repo.STATUS_PENDING,
            instruction_id=instruction_id,
            ts=now,
        )

        try:
            broker_order_id = await self.trade_port.submit_order(instruction)
        except Exception as exc:  # noqa: BLE001 — a broker reject must not kill the round
            repo.update_order(self.session, client_order_id, status=repo.STATUS_REJECTED)
            self.rejected_count += 1
            return SubmitResult(client_order_id, REJECTED, message=f"{type(exc).__name__}: {exc}")

        repo.update_order(
            self.session,
            client_order_id,
            status=repo.STATUS_OPEN,
            broker_order_id=broker_order_id,
        )
        repo.record_broker_usage(self.session, provider=self.broker_name, now=now)
        self.submitted_count += 1

        deadline = (
            now + timedelta(seconds=instruction.valid_seconds)
            if instruction.valid_seconds > 0
            else None
        )
        self._inflight[client_order_id] = _Inflight(
            client_order_id=client_order_id,
            order_row_id=order.id,
            symbol=instruction.symbol,
            action=instruction.action,
            quantity=instruction.quantity,
            limit_price=instruction.limit_price,
            deadline=deadline,
        )
        return SubmitResult(client_order_id, SUBMITTED, broker_order_id=broker_order_id)

    async def submit_batch(
        self, items: list[tuple[TradingInstruction, int]], *, now: datetime
    ) -> list[SubmitResult]:
        """Submit allowed instructions in order, then drain the resulting updates."""
        results = [
            await self.submit(instruction, instruction_id, now=now)
            for instruction, instruction_id in items
        ]
        await self.poll_updates(now=now)
        return results

    # -- order updates -------------------------------------------------------

    async def poll_updates(self, *, now: datetime) -> list[OrderUpdate]:
        """Persist every order update the broker has for us since the last poll."""
        updates = await self.trade_port.poll_order_updates()
        for update in updates:
            self._apply_update(update, now=now)
        return updates

    def _apply_update(self, update: OrderUpdate, *, now: datetime) -> None:
        order = repo.get_order(self.session, update.client_order_id)
        if order is None:
            # An order this DB has never seen (e.g. placed by an earlier run
            # against a different database). Record it so the audit trail is
            # complete rather than silently dropping a real fill.
            order = repo.record_order(
                self.session,
                client_order_id=update.client_order_id,
                status=update.status,
                broker_order_id=update.broker_order_id,
                ts=now,
            )

        if update.broker_fill_id and update.filled_quantity > 0:
            fill = repo.record_fill(
                self.session,
                order_id=order.id,
                broker_fill_id=update.broker_fill_id,
                quantity=update.filled_quantity,
                price=update.fill_price if update.fill_price is not None else 0.0,
                fee=update.fee,
                ts=now,
            )
            if fill is not None:  # None => this broker fill id was already recorded
                symbol, action = self._identify(update.client_order_id, order)
                if symbol is not None and action is not None:
                    repo.apply_fill_to_position(
                        self.session,
                        symbol=symbol,
                        action=action,
                        quantity=update.filled_quantity,
                        price=fill.price,
                    )
                inflight = self._inflight.get(update.client_order_id)
                if inflight is not None:
                    inflight.filled_quantity += update.filled_quantity
                self.filled_count += 1

        repo.update_order(
            self.session,
            update.client_order_id,
            status=update.status,
            broker_order_id=update.broker_order_id or None,
        )
        if update.status == repo.STATUS_CANCELLED:
            self.cancelled_count += 1
        if update.status in (repo.STATUS_FILLED, repo.STATUS_CANCELLED, repo.STATUS_REJECTED):
            self._inflight.pop(update.client_order_id, None)

    def _identify(self, client_order_id: str, order: Any) -> tuple[str | None, Action | None]:
        """Symbol and side for an order — from memory, else from its instruction row."""
        inflight = self._inflight.get(client_order_id)
        if inflight is not None:
            return inflight.symbol, inflight.action
        if order.instruction_id is None:
            return None, None
        instruction = self.session.get(Instruction, order.instruction_id)
        if instruction is None or not instruction.symbol:
            return None, None
        return instruction.symbol, Action[instruction.action]

    # -- our TIF -------------------------------------------------------------

    async def expire_timed_orders(self, *, now: datetime) -> list[str]:
        """Cancel the unfilled remainder of every order past its TIF (§5, §8)."""
        expired = [
            inflight.client_order_id
            for inflight in self._inflight.values()
            if inflight.deadline is not None and now >= inflight.deadline and inflight.remaining > 0
        ]
        return await self._cancel(expired, now=now)

    async def cancel_all_inflight(self, *, now: datetime) -> list[str]:
        """Graceful-shutdown path (§9.2) — cancel everything still working."""
        return await self._cancel(list(self._inflight), now=now)

    async def _cancel(self, client_order_ids: list[str], *, now: datetime) -> list[str]:
        cancelled: list[str] = []
        for client_order_id in client_order_ids:
            try:
                await self.trade_port.cancel_order(client_order_id)
            except Exception:  # noqa: BLE001 — already filled/cancelled at the broker
                self._inflight.pop(client_order_id, None)
                continue
            cancelled.append(client_order_id)
        await self.poll_updates(now=now)
        # A broker that does not emit a cancel update still leaves us
        # authoritative about what we asked for.
        for client_order_id in cancelled:
            order = repo.get_order(self.session, client_order_id)
            if order is not None and order.status in repo.LIVE_STATUSES:
                repo.update_order(self.session, client_order_id, status=repo.STATUS_CANCELLED)
                self.cancelled_count += 1
            self._inflight.pop(client_order_id, None)
        return cancelled

    # -- startup reconcile ---------------------------------------------------

    def adopt(self, snapshot: OrderSnapshot, *, now: datetime) -> None:
        """Track an order the broker reports as open (§9.1 step 3).

        It gets no TIF deadline — we do not know what window a previous run
        gave it — so it is never auto-cancelled, only cancelled on shutdown if
        `cancel_open_orders_on_shutdown` says so.
        """
        order = repo.record_order(
            self.session,
            client_order_id=snapshot.client_order_id,
            status=snapshot.status,
            broker_order_id=snapshot.broker_order_id,
            ts=now,
        )
        repo.update_order(self.session, snapshot.client_order_id, status=snapshot.status)
        symbol, action = self._identify(snapshot.client_order_id, order)
        self._inflight[snapshot.client_order_id] = _Inflight(
            client_order_id=snapshot.client_order_id,
            order_row_id=order.id,
            symbol=snapshot.symbol or symbol or "",
            action=action or Action.BUY,
            quantity=0,
            limit_price=0.0,
            deadline=None,
        )
