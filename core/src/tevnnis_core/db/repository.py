"""All §7 persistence for the decision loop — core is the only DB writer.

Every function takes a `Session` and only add()s / flush()es: the transaction
boundary belongs to the caller, which is the agent loop (one atomic round) and
the CLI (startup reconcile, shutdown flush).

Dedup lives in DB constraints, not in code (§7): `events.event_id`,
`orders.client_order_id` and `fills.broker_fill_id` are unique, and the helpers
here check-then-insert so a duplicate is a quiet no-op rather than an
IntegrityError that would roll back an otherwise good round.

Daily/weekly aggregates feed the §11 RiskContext. Two conventions, chosen to
match what `risk::ApplyAllowed` projects onto provisional state so the pre-trade
gates and the persisted history cannot disagree:

  * trade count and turnover are charged at **submission**, from
    `quantity x limit_price` of every non-rejected order submitted today — not
    from fills;
  * a symbol counts as *opened today* once a BUY has been **submitted** for it,
    which can only over-count day trades (the conservative direction for PDT).
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tevnnis_core.db.models import (
    DEFAULT_CURSOR_ID,
    ApiUsage,
    Event,
    Fill,
    Instruction,
    MdCursor,
    Order,
    PortfolioSample,
    Position,
    PriceSample,
    RiskAudit,
)
from tevnnis_core.instructions import Action
from tevnnis_core.ports import PositionSnapshot
from tevnnis_core.timeutil import day_bounds_utc, local_now, week_bounds_utc

# Order status vocabulary (free text in §7; fixed here so queries can rely on it).
STATUS_PENDING = "pending"
STATUS_OPEN = "open"
STATUS_PARTIALLY_FILLED = "partially_filled"
STATUS_FILLED = "filled"
STATUS_CANCELLED = "cancelled"
STATUS_REJECTED = "rejected"

#: Statuses that still have an unfilled remainder the broker could act on.
LIVE_STATUSES = (STATUS_PENDING, STATUS_OPEN, STATUS_PARTIALLY_FILLED)


# ---------------------------------------------------------------------------
# events (§7 — dedup on event_id, news also on news_id)
# ---------------------------------------------------------------------------


def persist_events(session: Session, records: Sequence[Any]) -> int:
    """Insert new event rows; return how many were actually new.

    md dedups in-memory over a rolling window, so a re-pull after an md restart
    can legitimately resend an event we already hold — the unique constraint is
    the backstop and this is the check in front of it.
    """
    if not records:
        return 0
    incoming = [r.event_id for r in records]
    existing = set(
        session.execute(select(Event.event_id).where(Event.event_id.in_(incoming)))
        .scalars()
        .all()
    )
    seen_news = set(
        session.execute(
            select(Event.news_id).where(
                Event.news_id.in_([r.news_id for r in records if r.news_id])
            )
        )
        .scalars()
        .all()
    )

    inserted = 0
    for record in records:
        if record.event_id in existing:
            continue
        news_id = record.news_id
        if news_id and news_id in seen_news:
            # Same story, a different event id: keep the event, drop the
            # duplicate news key so the unique constraint still holds.
            news_id = None
        session.add(
            Event(
                event_id=record.event_id,
                news_id=news_id,
                type=record.type,
                symbol=record.symbol,
                sector=record.sector,
                priority=record.priority,
                event_ts=record.event_ts,
                ingest_ts=record.ingest_ts,
                payload=record.payload,
            )
        )
        existing.add(record.event_id)
        if news_id:
            seen_news.add(news_id)
        inserted += 1

    session.flush()
    return inserted


# ---------------------------------------------------------------------------
# instructions + risk audit
# ---------------------------------------------------------------------------


def persist_instructions(session: Session, decision_id: str, unified: Any) -> list[int]:
    """Persist one row per unified instruction; return their ids, in order.

    `thesis` / `cited_event_ids` come off the unifier's `persistable` half —
    they are recorded here and never handed to Execution (§5).
    """
    ids: list[int] = []
    for item in unified.instructions:
        instruction = item.trading_instruction
        row = Instruction(
            decision_id=decision_id,
            action=instruction.action.name,
            symbol=instruction.symbol,
            order_type=instruction.order_type.name,
            quantity=instruction.quantity,
            limit_price=instruction.limit_price,
            valid_seconds=instruction.valid_seconds,
            confidence=instruction.confidence,
            thesis=item.persistable.thesis,
            cited_event_ids=list(item.persistable.cited_event_ids),
        )
        session.add(row)
        session.flush()
        ids.append(row.id)
    return ids


def persist_risk_audit(
    session: Session, instruction_id: int, *, allow: bool, rule_id: str, ts: datetime | None = None
) -> RiskAudit:
    """One row per risk check — allow *and* reject are both recorded (§7)."""
    row = RiskAudit(instruction_id=instruction_id, allow=allow, rule_tripped=rule_id)
    if ts is not None:
        row.ts = ts
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# orders (§7 — dedup on client_order_id)
# ---------------------------------------------------------------------------


def get_order(session: Session, client_order_id: str) -> Order | None:
    return session.execute(
        select(Order).where(Order.client_order_id == client_order_id)
    ).scalar_one_or_none()


def record_order(
    session: Session,
    *,
    client_order_id: str,
    status: str,
    instruction_id: int | None = None,
    broker_order_id: str | None = None,
    ts: datetime | None = None,
) -> Order:
    """Insert an order row, or return the existing one for this idempotency key."""
    existing = get_order(session, client_order_id)
    if existing is not None:
        return existing
    row = Order(
        client_order_id=client_order_id,
        broker_order_id=broker_order_id,
        instruction_id=instruction_id,
        status=status,
    )
    if ts is not None:
        row.ts = ts
    session.add(row)
    session.flush()
    return row


def update_order(
    session: Session,
    client_order_id: str,
    *,
    status: str | None = None,
    broker_order_id: str | None = None,
) -> Order | None:
    row = get_order(session, client_order_id)
    if row is None:
        return None
    if status is not None:
        row.status = status
    if broker_order_id is not None:
        row.broker_order_id = broker_order_id
    session.flush()
    return row


def live_orders(session: Session) -> list[Order]:
    """Orders the DB still believes are working (pending/open/partially filled)."""
    return list(
        session.execute(select(Order).where(Order.status.in_(LIVE_STATUSES))).scalars().all()
    )


# ---------------------------------------------------------------------------
# fills (§7 — dedup on broker_fill_id)
# ---------------------------------------------------------------------------


def record_fill(
    session: Session,
    *,
    order_id: int,
    broker_fill_id: str,
    quantity: int,
    price: float,
    fee: float = 0.0,
    ts: datetime | None = None,
) -> Fill | None:
    """Insert a fill; return None if this broker fill id was already recorded."""
    existing = session.execute(
        select(Fill).where(Fill.broker_fill_id == broker_fill_id)
    ).scalar_one_or_none()
    if existing is not None:
        return None
    row = Fill(
        order_id=order_id,
        broker_fill_id=broker_fill_id,
        quantity=quantity,
        price=price,
        fee=fee,
    )
    if ts is not None:
        row.ts = ts
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# positions (§7 — DB is a cache/audit; the broker is the source of truth)
# ---------------------------------------------------------------------------


def get_positions(session: Session) -> list[Position]:
    return list(session.execute(select(Position)).scalars().all())


def apply_fill_to_position(
    session: Session, *, symbol: str, action: Action, quantity: int, price: float
) -> None:
    """Move the position cache by one fill (weighted average cost on a BUY)."""
    row = session.execute(
        select(Position).where(Position.symbol == symbol)
    ).scalar_one_or_none()

    if action == Action.BUY:
        if row is None:
            session.add(Position(symbol=symbol, quantity=quantity, cost_basis=price))
        else:
            new_quantity = row.quantity + quantity
            if new_quantity > 0:
                row.cost_basis = (
                    row.cost_basis * row.quantity + price * quantity
                ) / new_quantity
            row.quantity = new_quantity
    else:  # SELL — cost basis per share is unchanged by a sale
        if row is None:
            return
        row.quantity -= quantity
        if row.quantity <= 0:
            session.delete(row)
    session.flush()


def mirror_broker_positions(
    session: Session, snapshots: Iterable[PositionSnapshot]
) -> list[str]:
    """Overwrite the position cache from the broker; return human-readable divergences.

    §7/§9.1: the broker is the source of truth. Anything the DB believed that
    the broker does not confirm is reported and then discarded.
    """
    broker = {s.symbol: s for s in snapshots}
    cached = {p.symbol: p for p in get_positions(session)}
    divergences: list[str] = []

    for symbol, snapshot in broker.items():
        row = cached.get(symbol)
        if row is None:
            divergences.append(
                f"{symbol}: broker holds {snapshot.quantity}, DB had no position"
            )
            session.add(
                Position(
                    symbol=symbol,
                    quantity=snapshot.quantity,
                    cost_basis=snapshot.cost_basis,
                )
            )
            continue
        if row.quantity != snapshot.quantity:
            divergences.append(
                f"{symbol}: broker holds {snapshot.quantity}, DB had {row.quantity}"
            )
        row.quantity = snapshot.quantity
        row.cost_basis = snapshot.cost_basis

    for symbol, row in cached.items():
        if symbol not in broker:
            divergences.append(f"{symbol}: DB had {row.quantity}, broker holds none")
            session.delete(row)

    session.flush()
    return divergences


# ---------------------------------------------------------------------------
# Samples that back the public snapshot time series.
# ---------------------------------------------------------------------------


def record_samples(
    session: Session,
    *,
    now: datetime,
    equity: float,
    cash: float,
    prices: Mapping[str, tuple[float, float | None]],
) -> None:
    """Append one equity point and one price point per symbol.

    Called once per decision round. The equity figure is dollars and stays
    private to the DB — `tevnnis_core.snapshot` publishes only the indexed
    ratio. Prices are public market data.

    A non-finite or non-positive equity is skipped rather than stored: a single
    bad reading would otherwise become a permanent spike in the published curve,
    or (as the first sample of a window) the divisor for every index in it.
    """
    if math.isfinite(equity) and equity > 0:
        session.add(PortfolioSample(ts=now, equity=float(equity), cash=float(cash)))

    for symbol, (last, change_pct) in prices.items():
        if not math.isfinite(last) or last <= 0:
            continue
        session.add(
            PriceSample(
                ts=now,
                symbol=symbol,
                last=float(last),
                change_pct=(
                    float(change_pct)
                    if change_pct is not None and math.isfinite(change_pct)
                    else None
                ),
            )
        )
    session.flush()


# ---------------------------------------------------------------------------
# md cursor (§4.3 — durable across a core restart)
# ---------------------------------------------------------------------------


def load_cursor(session: Session) -> str:
    row = session.get(MdCursor, DEFAULT_CURSOR_ID)
    return row.cursor if row is not None else ""


def save_cursor(session: Session, cursor: str, *, ts: datetime | None = None) -> None:
    row = session.get(MdCursor, DEFAULT_CURSOR_ID)
    if row is None:
        row = MdCursor(id=DEFAULT_CURSOR_ID, cursor=cursor)
        if ts is not None:
            row.updated_at = ts
        session.add(row)
    else:
        row.cursor = cursor
        if ts is not None:
            row.updated_at = ts
    session.flush()


# ---------------------------------------------------------------------------
# api_usage — the broker half of the §7 ledger
# ---------------------------------------------------------------------------


def record_broker_usage(
    session: Session, *, provider: str, call_count: int = 1, now: datetime | None = None
) -> None:
    row = ApiUsage(kind="broker", provider=provider, call_count=call_count, cost=0.0)
    if now is not None:
        row.ts = now
    session.add(row)
    session.flush()


# ---------------------------------------------------------------------------
# RiskContext aggregates (§11)
# ---------------------------------------------------------------------------


def _orders_with_instructions(
    session: Session, start: datetime, end: datetime
) -> list[tuple[Order, Instruction | None]]:
    rows = session.execute(
        select(Order, Instruction)
        .outerjoin(Instruction, Instruction.id == Order.instruction_id)
        .where(Order.ts >= start, Order.ts <= end, Order.status != STATUS_REJECTED)
        .order_by(Order.ts, Order.id)
    ).all()
    return [(order, instruction) for order, instruction in rows]


def trades_and_turnover_today(
    session: Session, *, now: datetime, tz: str
) -> tuple[int, float]:
    """Today's submitted (non-rejected) order count and their notional.

    An order adopted from the broker with no local instruction row counts
    toward the trade count but contributes no turnover — its notional is
    genuinely unknown to us, and inventing one would corrupt a hard fee gate.
    """
    start, end = day_bounds_utc(now, tz)
    pairs = _orders_with_instructions(session, start, end)
    turnover = sum(
        (instruction.quantity or 0) * (instruction.limit_price or 0.0)
        for _, instruction in pairs
        if instruction is not None
    )
    return len(pairs), float(turnover)


def positions_opened_today(session: Session, *, now: datetime, tz: str) -> set[str]:
    """Symbols with a BUY submitted today — a SELL of one of these is a day trade."""
    start, end = day_bounds_utc(now, tz)
    return {
        instruction.symbol
        for _, instruction in _orders_with_instructions(session, start, end)
        if instruction is not None
        and instruction.action == Action.BUY.name
        and instruction.symbol
    }


def day_trades_this_week(session: Session, *, now: datetime, tz: str) -> int:
    """Day trades used so far this week — a SELL after a same-day BUY of that symbol."""
    start, end = week_bounds_utc(now, tz)
    bought_on: dict[Any, set[str]] = defaultdict(set)
    count = 0
    for order, instruction in _orders_with_instructions(session, start, end):
        if instruction is None or not instruction.symbol:
            continue
        ts = order.ts if order.ts.tzinfo else order.ts.replace(tzinfo=start.tzinfo)
        day = local_now(ts, tz).date()
        if instruction.action == Action.BUY.name:
            bought_on[day].add(instruction.symbol)
        elif instruction.action == Action.SELL.name and instruction.symbol in bought_on[day]:
            count += 1
    return count


def seen_client_order_ids(session: Session, *, now: datetime, tz: str) -> set[str]:
    """Idempotency keys Risk must treat as already used (§5): today's plus anything live."""
    start, end = day_bounds_utc(now, tz)
    return set(
        session.execute(
            select(Order.client_order_id).where(
                (Order.ts >= start) & (Order.ts <= end) | Order.status.in_(LIVE_STATUSES)
            )
        )
        .scalars()
        .all()
    )


def fills_summary_today(session: Session, *, now: datetime, tz: str) -> tuple[int, float]:
    """Today's fill count and total fees — for the end-of-session summary."""
    start, end = day_bounds_utc(now, tz)
    count, fees = session.execute(
        select(func.count(Fill.id), func.coalesce(func.sum(Fill.fee), 0.0)).where(
            Fill.ts >= start, Fill.ts <= end
        )
    ).one()
    return int(count), float(fees)
