"""Acceptance test — a scripted scenario driven through the whole v0 pipeline.

Everything is a mock (§13): md is a scripted `PullResponse` list, the LLM is a
scripted `DecisionOutput` list, the broker is in-memory. The scenario file is
the one the repo ships for the guided first run
(`config/scenario.example.json`), so the demo the operator runs and the
behaviour asserted here cannot drift apart.

The path exercised is the real CLI lifecycle — `validate_dependencies` ->
`reconcile` -> `confirm_start` -> `run_loop` -> `shutdown` — with only the
clock injected, so the §9 sequence itself is under test rather than a
test-only re-assembly of it.

    round 1: a MEDIUM news batch  -> cheap gate HOLDs, no LLM call, no order
    round 2: a HIGH quote move    -> model proposes a BUY, risk allows it,
                                     the order is submitted and fills
"""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path

import pytest

from tevnnis_core import cli
from tevnnis_core.agent import GATE_HOLD_LOW_PRIORITY, GATE_OK
from tevnnis_core.db import repository as repo
from tevnnis_core.db.models import (
    ApiUsage,
    Decision,
    Event,
    Fill,
    Instruction,
    Order,
    Position,
    RiskAudit,
)
from tevnnis_core.llm.instruction_unifier import make_client_order_id
from tevnnis_core.mocks.broker import FillMode
from tevnnis_core.mocks.scenario import load_scenario

risk = pytest.importorskip(
    "tevnnis_risk",
    reason="build the extension first: cmake -S . -B build && cmake --build build -j",
)

SCENARIO = Path(__file__).resolve().parents[2] / "config" / "scenario.example.json"


@pytest.fixture
def scenario():
    return load_scenario(SCENARIO)


def deps_for(config, session, scenario):
    return cli.build_dependencies(
        config,
        session=session,
        md_client=scenario.md_client,
        trade_port=scenario.broker,
        llm_provider=scenario.llm,
        risk_module=risk,
    )


async def run_session(config, session, scenario, now, *, rounds: int = 2):
    """The §9 lifecycle, start to finish, with only the clock injected."""
    out = io.StringIO()
    deps = deps_for(config, session, scenario)

    await cli.validate_dependencies(deps)
    reconciled = await cli.reconcile(deps, now=now)
    summary = cli.render_startup_summary(deps, reconciled, now=now)
    assert cli.confirm_start(deps, assume_yes=True, out=out)

    clock = [now + timedelta(seconds=i) for i in range(rounds + 1)]
    stats = await cli.run_loop(
        deps,
        stop=cli.StopFlag(),
        max_rounds=rounds,
        now_fn=lambda: clock.pop(0),
        sleep=_no_sleep,
        out=out,
    )
    cancelled = await cli.shutdown(
        deps, stats, now=now, reason="test run complete", out=out
    )
    return deps, stats, cancelled, out.getvalue(), reconciled, summary


async def _no_sleep(seconds: float) -> None:
    return None


async def test_scenario_produces_the_expected_orders_and_db_records(
    config, full_db_session, scenario, now
):
    deps, stats, cancelled, output, reconciled, _ = await run_session(
        config, full_db_session, scenario, now
    )
    session = full_db_session

    # --- decisions: both rounds logged, only one spent tokens (§7, §8) ------
    decisions = session.query(Decision).order_by(Decision.ts).all()
    assert len(decisions) == 2
    held, decided = sorted(decisions, key=lambda d: d.gate_result != GATE_OK)[::-1]
    assert held.gate_result == GATE_HOLD_LOW_PRIORITY
    assert held.tokens_in is None and held.tokens_out is None
    assert decided.gate_result == GATE_OK
    assert (decided.tokens_in, decided.tokens_out) == (100, 50)
    assert decided.model_used == "mock"
    assert "NVDA" in decided.session_note or decided.session_note

    # --- events: both rounds' events persisted for audit --------------------
    events = {e.event_id: e for e in session.query(Event).all()}
    assert set(events) == {"evt-news-1", "evt-quote-1"}
    assert events["evt-news-1"].news_id == "n-1"
    assert events["evt-news-1"].priority == "MEDIUM"
    assert events["evt-quote-1"].payload["trigger"] == "cross_+3pct"

    # --- instructions: one BUY, with its thesis and citations ---------------
    instruction = session.query(Instruction).one()
    assert (instruction.action, instruction.symbol, instruction.quantity) == (
        "BUY",
        "NVDA.US",
        10,
    )
    assert instruction.limit_price == 104.0
    assert instruction.valid_seconds == 300
    assert instruction.cited_event_ids == ["evt-quote-1"]
    assert "band" in instruction.thesis
    assert instruction.decision_id == decided.decision_id

    # --- risk audit: one allow, recorded against that instruction -----------
    audit = session.query(RiskAudit).one()
    assert (audit.allow, audit.rule_tripped) == (True, "ALLOWED")
    assert audit.instruction_id == instruction.id

    # --- orders: one order, id derived from the decision identity (§5) ------
    order = session.query(Order).one()
    assert order.client_order_id == make_client_order_id(decided.decision_id, 0)
    assert order.broker_order_id == "MOCK-1"
    assert order.status == repo.STATUS_FILLED
    assert order.instruction_id == instruction.id

    # --- fills: one fill, keyed on the broker's own id ----------------------
    fill = session.query(Fill).one()
    assert (fill.quantity, fill.price) == (10, 104.0)
    assert fill.broker_fill_id == "MOCK-1-F1"
    assert fill.order_id == order.id

    # --- positions: the pre-existing holding plus the new one ---------------
    positions = {p.symbol: p for p in session.query(Position).all()}
    assert set(positions) == {"AMD.US", "NVDA.US"}
    assert (positions["NVDA.US"].quantity, positions["NVDA.US"].cost_basis) == (10, 104.0)
    assert positions["AMD.US"].quantity == 20

    # --- api_usage: one llm row and one broker row (§7 ledger) --------------
    usage = session.query(ApiUsage).all()
    assert sorted(u.kind for u in usage) == ["broker", "llm"]
    llm_usage = next(u for u in usage if u.kind == "llm")
    assert (llm_usage.tokens_in, llm_usage.tokens_out, llm_usage.call_count) == (100, 50, 1)

    # --- the broker really moved -------------------------------------------
    account = await deps.trade_port.query_account()
    assert account.cash == pytest.approx(10_000.0 - 10 * 104.0 - 0.5)
    assert cancelled == []  # the order filled; nothing was left working

    # --- cursor advanced and persisted (§4.3) -------------------------------
    assert repo.load_cursor(session) == "c2"
    assert [r.since_cursor for r in scenario.md_client.requests] == ["", "c1"]

    # --- the operator was told what happened --------------------------------
    assert "HOLD_LOW_PRIORITY" in output
    assert "below reason_min_priority=HIGH" in output
    assert "ORDER" in output and "submitted" in output
    assert "end of session" in output
    assert stats.gate_results[GATE_OK] == 1
    assert stats.submitted == 1

    # --- startup reconcile saw the pre-existing holding ---------------------
    assert any("AMD.US" in d for d in reconciled.divergences)


async def test_the_hold_round_places_no_order_and_costs_nothing(
    config, full_db_session, scenario, now
):
    """Round 1 alone: HOLD is logged, no LLM call, no order, no usage."""
    deps = deps_for(config, full_db_session, scenario)
    await cli.reconcile(deps, now=now)

    stats = await cli.run_loop(
        deps,
        stop=cli.StopFlag(),
        max_rounds=1,
        now_fn=lambda: now,
        sleep=_no_sleep,
        out=io.StringIO(),
    )

    assert stats.rounds == 1
    assert stats.submitted == 0
    decision = full_db_session.query(Decision).one()
    assert decision.gate_result == GATE_HOLD_LOW_PRIORITY
    assert scenario.llm.call_count == 0
    assert full_db_session.query(Order).count() == 0
    assert full_db_session.query(Instruction).count() == 0
    assert full_db_session.query(ApiUsage).count() == 0
    assert full_db_session.query(Event).count() == 1  # still audited


async def test_a_restart_mid_scenario_neither_replays_events_nor_double_orders(
    config, full_db_session, scenario, now
):
    """§9.3 — reconcile + a persisted cursor + a deterministic id make restarts safe."""
    deps = deps_for(config, full_db_session, scenario)
    await cli.reconcile(deps, now=now)
    await cli.run_loop(
        deps,
        stop=cli.StopFlag(),
        max_rounds=2,
        now_fn=lambda: now,
        sleep=_no_sleep,
        out=io.StringIO(),
    )
    orders_before = full_db_session.query(Order).count()

    # A second process against the same DB and broker: same scenario replayed
    # from the top, but the cursor and the idempotency keys are already stored.
    restarted = load_scenario(SCENARIO)
    restarted.broker = scenario.broker  # the broker keeps its state across our restart
    deps2 = deps_for(config, full_db_session, restarted)
    await cli.reconcile(deps2, now=now)
    await cli.run_loop(
        deps2,
        stop=cli.StopFlag(),
        max_rounds=2,
        now_fn=lambda: now,
        sleep=_no_sleep,
        out=io.StringIO(),
    )

    # Resumed from the stored cursor: md serves nothing new, so there is no
    # second decision on an event we already acted on, and no second order.
    assert restarted.md_client.requests[0].since_cursor == "c2"
    assert full_db_session.query(Order).count() == orders_before == 1
    assert full_db_session.query(Fill).count() == 1
    assert full_db_session.query(Event).count() == 2  # nothing re-inserted
    assert {p.symbol: p.quantity for p in full_db_session.query(Position).all()} == {
        "AMD.US": 20,
        "NVDA.US": 10,
    }


async def test_an_unfilled_order_is_cancelled_by_the_tif_then_by_shutdown(
    config, full_db_session, scenario, now
):
    """The same scenario against a broker that never fills (§8 step 8, §9.2)."""
    scenario.broker.fill_mode = FillMode.NO_FILL
    deps = deps_for(config, full_db_session, scenario)
    out = io.StringIO()
    await cli.reconcile(deps, now=now)

    clock = [now, now + timedelta(seconds=1)]
    await cli.run_loop(
        deps,
        stop=cli.StopFlag(),
        max_rounds=2,
        now_fn=lambda: clock.pop(0),
        sleep=_no_sleep,
        out=out,
    )

    order = full_db_session.query(Order).one()
    assert order.status == repo.STATUS_OPEN
    assert full_db_session.query(Fill).count() == 0
    assert deps.execution.inflight_ids == [order.client_order_id]

    # Past the 300s TIF: the unfilled remainder is cancelled.
    expired = await deps.execution.expire_timed_orders(now=now + timedelta(seconds=301))
    assert expired == [order.client_order_id]
    assert repo.get_order(full_db_session, order.client_order_id).status == (
        repo.STATUS_CANCELLED
    )

    cancelled = await cli.shutdown(
        deps, cli.SessionStats(), now=now, reason="test", out=out
    )
    assert cancelled == []  # nothing left working — the TIF already handled it
