"""Persistence helpers and the §11 aggregates (§7 — core is the only writer)."""

from __future__ import annotations

from datetime import timedelta

from tevnnis_core.db import repository as repo
from tevnnis_core.db.models import Event, Fill, Instruction, Order, Position
from tevnnis_core.instructions import Action, OrderType, TradingInstruction
from tevnnis_core.llm.instruction_unifier import unify
from tevnnis_core.market_data import EventRecord
from tevnnis_core.mocks.llm_scenarios import buy_scenario
from tevnnis_core.ports import PositionSnapshot

TZ = "America/New_York"


def record(event_id: str, *, news_id: str | None = None, priority: str = "HIGH") -> EventRecord:
    return EventRecord(
        event_id=event_id,
        type="NEWS" if news_id else "QUOTE_MOVE",
        priority=priority,
        event_ts=1,
        ingest_ts=2,
        symbol="NVDA.US",
        sector="Semiconductor",
        news_id=news_id,
        payload={"trigger": "cross_+3pct"},
    )


def make_instruction(
    session, *, action=Action.BUY, symbol="NVDA.US", quantity=10, limit_price=100.0
) -> Instruction:
    row = Instruction(
        decision_id="d-1",
        action=action.name,
        symbol=symbol,
        order_type="LIMIT",
        quantity=quantity,
        limit_price=limit_price,
        valid_seconds=300,
        confidence=0.8,
        cited_event_ids=[],
    )
    session.add(row)
    session.flush()
    return row


# --- events ---------------------------------------------------------------


def test_persist_events_inserts_and_dedups_on_event_id(full_db_session):
    assert repo.persist_events(full_db_session, [record("e1"), record("e2")]) == 2
    # A re-pull after an md restart legitimately resends what we already hold.
    assert repo.persist_events(full_db_session, [record("e1"), record("e3")]) == 1
    assert full_db_session.query(Event).count() == 3


def test_persist_events_keeps_the_event_when_only_news_id_repeats(full_db_session):
    repo.persist_events(full_db_session, [record("e1", news_id="n-1")])
    repo.persist_events(full_db_session, [record("e2", news_id="n-1")])

    rows = full_db_session.query(Event).order_by(Event.event_id).all()
    assert [r.event_id for r in rows] == ["e1", "e2"]
    # The unique news key stays on the first event; the duplicate drops it.
    assert [r.news_id for r in rows] == ["n-1", None]


def test_persist_events_stores_the_payload_as_json(full_db_session):
    repo.persist_events(full_db_session, [record("e1")])
    assert full_db_session.query(Event).one().payload == {"trigger": "cross_+3pct"}


# --- instructions + risk audit ---------------------------------------------


def test_persist_instructions_splits_thesis_and_cited_events_into_the_row(full_db_session):
    from tevnnis_core.db.models import Decision

    full_db_session.add(Decision(decision_id="d-1", gate_result="OK"))
    full_db_session.flush()

    unified = unify("d-1", buy_scenario(symbol="NVDA.US", cited_event_ids=["e1", "e2"]))
    ids = repo.persist_instructions(full_db_session, "d-1", unified)

    row = full_db_session.get(Instruction, ids[0])
    assert row.action == "BUY"
    assert row.symbol == "NVDA.US"
    assert row.thesis == "scripted buy scenario"
    assert row.cited_event_ids == ["e1", "e2"]


def test_persist_risk_audit_records_rejections_too(full_db_session):
    instruction = make_instruction(full_db_session)
    repo.persist_risk_audit(
        full_db_session, instruction.id, allow=False, rule_id="MAX_ORDER_NOTIONAL"
    )
    audit = instruction.risk_audit[0]
    assert audit.allow is False
    assert audit.rule_tripped == "MAX_ORDER_NOTIONAL"


# --- orders / fills --------------------------------------------------------


def test_record_order_is_idempotent_on_client_order_id(full_db_session, now):
    first = repo.record_order(
        full_db_session, client_order_id="co_1", status=repo.STATUS_PENDING, ts=now
    )
    second = repo.record_order(
        full_db_session, client_order_id="co_1", status=repo.STATUS_OPEN, ts=now
    )
    assert first.id == second.id
    assert full_db_session.query(Order).count() == 1
    assert second.status == repo.STATUS_PENDING  # the existing row is returned, not overwritten


def test_record_fill_dedups_on_broker_fill_id(full_db_session, now):
    order = repo.record_order(
        full_db_session, client_order_id="co_1", status=repo.STATUS_OPEN, ts=now
    )
    assert repo.record_fill(
        full_db_session, order_id=order.id, broker_fill_id="F1", quantity=5, price=100.0
    ) is not None
    assert repo.record_fill(
        full_db_session, order_id=order.id, broker_fill_id="F1", quantity=5, price=100.0
    ) is None
    assert full_db_session.query(Fill).count() == 1


# --- positions -------------------------------------------------------------


def test_buy_fills_average_the_cost_basis(full_db_session):
    repo.apply_fill_to_position(
        full_db_session, symbol="NVDA.US", action=Action.BUY, quantity=10, price=100.0
    )
    repo.apply_fill_to_position(
        full_db_session, symbol="NVDA.US", action=Action.BUY, quantity=10, price=120.0
    )
    row = full_db_session.query(Position).one()
    assert row.quantity == 20
    assert row.cost_basis == 110.0


def test_selling_out_removes_the_position(full_db_session):
    repo.apply_fill_to_position(
        full_db_session, symbol="NVDA.US", action=Action.BUY, quantity=10, price=100.0
    )
    repo.apply_fill_to_position(
        full_db_session, symbol="NVDA.US", action=Action.SELL, quantity=10, price=130.0
    )
    assert full_db_session.query(Position).count() == 0


def test_mirror_broker_positions_reports_divergence_and_takes_the_broker_view(full_db_session):
    repo.apply_fill_to_position(
        full_db_session, symbol="NVDA.US", action=Action.BUY, quantity=10, price=100.0
    )
    repo.apply_fill_to_position(
        full_db_session, symbol="XOM.US", action=Action.BUY, quantity=5, price=100.0
    )

    divergences = repo.mirror_broker_positions(
        full_db_session,
        [
            PositionSnapshot(symbol="NVDA.US", quantity=7, cost_basis=101.0),
            PositionSnapshot(symbol="AMD.US", quantity=20, cost_basis=48.0),
        ],
    )

    symbols = {p.symbol: p for p in repo.get_positions(full_db_session)}
    assert set(symbols) == {"NVDA.US", "AMD.US"}  # XOM dropped: the broker does not hold it
    assert symbols["NVDA.US"].quantity == 7
    assert any("NVDA.US" in d and "DB had 10" in d for d in divergences)
    assert any("AMD.US" in d and "no position" in d for d in divergences)
    assert any("XOM.US" in d and "broker holds none" in d for d in divergences)


# --- md cursor -------------------------------------------------------------


def test_cursor_round_trips(full_db_session):
    assert repo.load_cursor(full_db_session) == ""
    repo.save_cursor(full_db_session, "c1")
    repo.save_cursor(full_db_session, "c2")
    assert repo.load_cursor(full_db_session) == "c2"


# --- risk aggregates -------------------------------------------------------


def submit(session, now, *, action=Action.BUY, symbol="NVDA.US", quantity=10, price=100.0,
           client_order_id="co_1", status=repo.STATUS_OPEN):
    instruction = make_instruction(
        session, action=action, symbol=symbol, quantity=quantity, limit_price=price
    )
    return repo.record_order(
        session,
        client_order_id=client_order_id,
        status=status,
        instruction_id=instruction.id,
        ts=now,
    )


def test_trades_and_turnover_count_submitted_orders_at_limit_notional(full_db_session, now):
    submit(full_db_session, now, quantity=10, price=100.0, client_order_id="co_1")
    submit(full_db_session, now, quantity=5, price=200.0, client_order_id="co_2")

    trades, turnover = repo.trades_and_turnover_today(full_db_session, now=now, tz=TZ)
    assert trades == 2
    assert turnover == 10 * 100.0 + 5 * 200.0


def test_rejected_orders_consume_no_trade_count_or_turnover(full_db_session, now):
    submit(full_db_session, now, client_order_id="co_1", status=repo.STATUS_REJECTED)
    assert repo.trades_and_turnover_today(full_db_session, now=now, tz=TZ) == (0, 0.0)


def test_yesterdays_orders_do_not_count_toward_today(full_db_session, now):
    submit(full_db_session, now - timedelta(days=1), client_order_id="co_old")
    assert repo.trades_and_turnover_today(full_db_session, now=now, tz=TZ) == (0, 0.0)


def test_positions_opened_today_lists_symbols_bought_today(full_db_session, now):
    submit(full_db_session, now, action=Action.BUY, symbol="NVDA.US", client_order_id="co_1")
    submit(full_db_session, now, action=Action.SELL, symbol="AMD.US", client_order_id="co_2")
    assert repo.positions_opened_today(full_db_session, now=now, tz=TZ) == {"NVDA.US"}


def test_day_trade_is_a_sell_after_a_same_day_buy_of_that_symbol(full_db_session, now):
    submit(full_db_session, now, action=Action.BUY, symbol="NVDA.US", client_order_id="co_1")
    submit(full_db_session, now, action=Action.SELL, symbol="NVDA.US", client_order_id="co_2")
    # A sell of a symbol bought on an earlier day is not a day trade.
    submit(
        full_db_session,
        now - timedelta(days=1),
        action=Action.BUY,
        symbol="AMD.US",
        client_order_id="co_3",
    )
    submit(full_db_session, now, action=Action.SELL, symbol="AMD.US", client_order_id="co_4")

    assert repo.day_trades_this_week(full_db_session, now=now, tz=TZ) == 1


def test_seen_client_order_ids_covers_today_and_anything_still_live(full_db_session, now):
    submit(full_db_session, now, client_order_id="co_today", status=repo.STATUS_FILLED)
    submit(
        full_db_session,
        now - timedelta(days=3),
        client_order_id="co_old_live",
        status=repo.STATUS_OPEN,
    )
    submit(
        full_db_session,
        now - timedelta(days=3),
        client_order_id="co_old_done",
        status=repo.STATUS_FILLED,
    )

    seen = repo.seen_client_order_ids(full_db_session, now=now, tz=TZ)
    assert seen == {"co_today", "co_old_live"}


def test_live_orders_are_the_ones_with_an_unfilled_remainder(full_db_session, now):
    submit(full_db_session, now, client_order_id="co_open", status=repo.STATUS_OPEN)
    submit(
        full_db_session, now, client_order_id="co_partial", status=repo.STATUS_PARTIALLY_FILLED
    )
    submit(full_db_session, now, client_order_id="co_filled", status=repo.STATUS_FILLED)
    submit(full_db_session, now, client_order_id="co_cancelled", status=repo.STATUS_CANCELLED)

    assert {o.client_order_id for o in repo.live_orders(full_db_session)} == {
        "co_open",
        "co_partial",
    }


def test_instruction_shape_is_preserved_through_a_canonical_instruction(full_db_session, now):
    """A guard on the Action/OrderType enum names the aggregates query against."""
    instruction = TradingInstruction(
        action=Action.SELL,
        symbol="NVDA.US",
        order_type=OrderType.LIMIT,
        quantity=3,
        limit_price=99.0,
        valid_seconds=60,
        confidence=0.5,
        client_order_id="co_x",
    )
    assert instruction.action.name == "SELL"
    assert instruction.order_type.name == "LIMIT"
