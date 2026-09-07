"""The §8 decision round: cheap gate, HOLD logging, risk, and execution hand-off."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tevnnis_core.agent import (
    GATE_HOLD_BUDGET,
    GATE_HOLD_KILL_SWITCH,
    GATE_HOLD_LOW_PRIORITY,
    GATE_HOLD_NO_EVENTS,
    GATE_OK,
    AgentLoop,
    cheap_gate,
)
from tevnnis_core.db import repository as repo
from tevnnis_core.db.models import ApiUsage, Decision, Event, Instruction, Order, RiskAudit
from tevnnis_core.execution import ExecutionEngine
from tevnnis_core.instructions import Action, DecisionOutput, OrderType, ProposedInstruction
from tevnnis_core.llm.budget import BudgetStatus
from tevnnis_core.llm.router import LLMRouter
from tevnnis_core.market_data import map_pull_response
from tevnnis_core.mocks.broker import FillMode, MockBroker
from tevnnis_core.mocks.llm import MockLLM
from tevnnis_core.mocks.market_data import MockMarketDataClient, make_event, make_response

risk = pytest.importorskip(
    "tevnnis_risk",
    reason="build the extension first: cmake -S . -B build && cmake --build build -j",
)

OK_BUDGET = BudgetStatus(ok=True, tokens_used_today=0, calls_used_today=0)
SPENT_BUDGET = BudgetStatus(
    ok=False, tokens_used_today=1, calls_used_today=99, reason="llm_daily_call_cap reached"
)

SNAPSHOTS = {
    "Semiconductor": [("NVDA.US", 104.0, 4.0), ("AMD.US", 50.0, 0.2)],
    "Web": [("GOOGL.US", 200.0, 1.0)],
}


def quote_event(event_id: str = "e1", priority: str = "HIGH", symbol: str = "NVDA.US"):
    return make_event(
        event_id,
        type="QUOTE_MOVE",
        symbol=symbol,
        sector="Semiconductor",
        priority=priority,
        quote={"last_price": 104.0, "prev_close": 100.0, "change_pct": 4.0,
               "trigger": "cross_+3pct"},
    )


def batch_of(*events, cursor: str = "c1"):
    return map_pull_response(
        make_response(events=list(events), snapshots=SNAPSHOTS, next_cursor=cursor)
    )


def buy_output(symbol: str = "NVDA.US", quantity: int = 10, limit_price: float = 104.0):
    return DecisionOutput(
        instructions=[
            ProposedInstruction(
                action=Action.BUY,
                symbol=symbol,
                order_type=OrderType.LIMIT,
                quantity=quantity,
                limit_price=limit_price,
                valid_seconds=300,
                confidence=0.8,
                thesis="crossed the +3% band",
                cited_event_ids=["e1"],
            )
        ],
        session_note="one entry",
    )


@dataclass
class Harness:
    loop: AgentLoop
    broker: MockBroker
    llm: MockLLM
    md: MockMarketDataClient


def harness(
    config,
    session,
    *,
    responses,
    llm_response,
    cash: float = 10_000.0,
    fill_mode: FillMode = FillMode.IMMEDIATE_FULL,
) -> Harness:
    broker = MockBroker(initial_cash=cash, fill_mode=fill_mode)
    llm = MockLLM(response=llm_response)
    md = MockMarketDataClient(responses)
    execution = ExecutionEngine(trade_port=broker, session=session, broker_name="mock")
    loop = AgentLoop(
        config=config,
        session=session,
        md_client=md,
        trade_port=broker,
        router=LLMRouter(
            provider=llm,
            budgets=config.budgets,
            tz=config.cadence.timezone,
            provider_name="mock",
        ),
        execution=execution,
        risk_module=risk,
    )
    return Harness(loop=loop, broker=broker, llm=llm, md=md)


# --- the cheap gate (pure) --------------------------------------------------


def test_gate_holds_on_an_empty_batch():
    outcome = cheap_gate(batch_of(), budget=OK_BUDGET, reason_min_priority="HIGH")
    assert (outcome.proceed, outcome.result) == (False, GATE_HOLD_NO_EVENTS)


def test_gate_holds_when_nothing_reaches_the_wake_threshold():
    outcome = cheap_gate(
        batch_of(quote_event(priority="MEDIUM")), budget=OK_BUDGET, reason_min_priority="HIGH"
    )
    assert (outcome.proceed, outcome.result) == (False, GATE_HOLD_LOW_PRIORITY)
    # The OPERATOR gets the concrete comparison, on the terminal.
    assert "MEDIUM" in outcome.reason and "HIGH" in outcome.reason


def test_the_published_gate_reason_carries_no_configuration_value():
    """`public_reason` is persisted, and a gated HOLD's note becomes a thesis.

    `reason_min_priority` is §6 cadence configuration. It must not travel to the
    public snapshot, and the fix is to keep it out of the persisted string in
    the first place — not to rely on a scrubber downstream noticing it.
    """
    outcome = cheap_gate(
        batch_of(quote_event(priority="MEDIUM")), budget=OK_BUDGET, reason_min_priority="HIGH"
    )

    published = outcome.published_reason
    assert "reason_min_priority" not in published
    assert "HIGH" not in published
    # The OBSERVATION survives — it is what actually arrived, not a setting.
    assert "MEDIUM" in published
    assert published != outcome.reason


@pytest.mark.parametrize("threshold", ["LOW", "MEDIUM", "HIGH", "CRITICAL"])
def test_no_configured_threshold_level_ever_reaches_the_published_reason(threshold):
    """Whatever the wake threshold is set to, it stays out of the note."""
    outcome = cheap_gate(
        batch_of(quote_event(priority="LOW")),
        budget=OK_BUDGET,
        reason_min_priority=threshold,
    )
    if not outcome.proceed and outcome.result == GATE_HOLD_LOW_PRIORITY:
        assert threshold not in outcome.published_reason


def test_gate_outcomes_without_configuration_publish_their_reason_unchanged():
    """Only the threshold comparison needs a separate public form."""
    for outcome in (
        cheap_gate(batch_of(), budget=OK_BUDGET, reason_min_priority="HIGH"),
        cheap_gate(batch_of(), budget=OK_BUDGET, reason_min_priority="HIGH", kill_switch=True),
    ):
        assert outcome.published_reason == outcome.reason


def test_gate_proceeds_on_a_high_priority_event():
    assert cheap_gate(
        batch_of(quote_event(priority="HIGH")), budget=OK_BUDGET, reason_min_priority="HIGH"
    ).proceed


def test_gate_proceeds_on_critical_and_respects_a_lowered_threshold():
    assert cheap_gate(
        batch_of(quote_event(priority="CRITICAL")), budget=OK_BUDGET, reason_min_priority="HIGH"
    ).proceed
    assert cheap_gate(
        batch_of(quote_event(priority="MEDIUM")), budget=OK_BUDGET, reason_min_priority="MEDIUM"
    ).proceed


def test_gate_checks_budget_before_priority():
    outcome = cheap_gate(
        batch_of(quote_event()), budget=SPENT_BUDGET, reason_min_priority="HIGH"
    )
    assert (outcome.proceed, outcome.result) == (False, GATE_HOLD_BUDGET)


def test_kill_switch_outranks_every_other_gate_reason():
    outcome = cheap_gate(
        batch_of(quote_event()),
        budget=SPENT_BUDGET,
        reason_min_priority="HIGH",
        kill_switch=True,
    )
    assert outcome.result == GATE_HOLD_KILL_SWITCH


# --- the round --------------------------------------------------------------


async def test_low_priority_batch_logs_a_hold_and_never_calls_the_llm(
    config, full_db_session, now
):
    h = harness(
        config,
        full_db_session,
        responses=[make_response(events=[quote_event(priority="MEDIUM")], next_cursor="c1")],
        llm_response=buy_output(),
    )

    report = await h.loop.run_round(now=now)

    assert report.gate_result == GATE_HOLD_LOW_PRIORITY
    assert h.llm.call_count == 0  # no strong-model tokens spent
    decision = full_db_session.query(Decision).one()
    assert decision.gate_result == GATE_HOLD_LOW_PRIORITY
    assert decision.tokens_in is None
    assert full_db_session.query(Order).count() == 0
    assert full_db_session.query(ApiUsage).count() == 0
    # The event is still persisted for audit even though we held.
    assert full_db_session.query(Event).count() == 1


async def test_empty_batch_logs_a_hold(config, full_db_session, now):
    h = harness(
        config, full_db_session, responses=[make_response()], llm_response=buy_output()
    )
    report = await h.loop.run_round(now=now)

    assert report.gate_result == GATE_HOLD_NO_EVENTS
    assert full_db_session.query(Decision).one().gate_result == GATE_HOLD_NO_EVENTS
    assert h.llm.call_count == 0


async def test_spent_budget_logs_a_hold_without_calling_the_provider(
    config, full_db_session, now
):
    config.budgets.llm_daily_call_cap = 1
    full_db_session.add(ApiUsage(kind="llm", provider="mock", call_count=1, ts=now))
    full_db_session.flush()

    h = harness(
        config,
        full_db_session,
        responses=[make_response(events=[quote_event()], next_cursor="c1")],
        llm_response=buy_output(),
    )
    report = await h.loop.run_round(now=now)

    assert report.gate_result == GATE_HOLD_BUDGET
    assert h.llm.call_count == 0


async def test_kill_switch_holds_the_round(config, full_db_session, now):
    h = harness(
        config,
        full_db_session,
        responses=[make_response(events=[quote_event()], next_cursor="c1")],
        llm_response=buy_output(),
    )
    h.loop.kill_switch = True

    report = await h.loop.run_round(now=now)

    assert report.gate_result == GATE_HOLD_KILL_SWITCH
    assert h.llm.call_count == 0
    assert full_db_session.query(Decision).one().gate_result == GATE_HOLD_KILL_SWITCH


async def test_an_empty_instruction_list_is_a_logged_hold_with_no_order(
    config, full_db_session, now
):
    h = harness(
        config,
        full_db_session,
        responses=[make_response(events=[quote_event()], next_cursor="c1")],
        llm_response=DecisionOutput(instructions=[], session_note="nothing worth doing"),
    )
    report = await h.loop.run_round(now=now)

    assert report.gate_result == GATE_OK
    assert report.is_hold
    decision = full_db_session.query(Decision).one()
    assert decision.gate_result == "OK"
    assert decision.session_note == "nothing worth doing"
    assert full_db_session.query(Instruction).count() == 0
    assert full_db_session.query(Order).count() == 0


async def test_happy_path_persists_the_whole_chain_and_submits_the_order(
    config, full_db_session, now
):
    h = harness(
        config,
        full_db_session,
        responses=[make_response(events=[quote_event()], snapshots=SNAPSHOTS, next_cursor="c1")],
        llm_response=buy_output(),
    )

    report = await h.loop.run_round(now=now)

    assert report.gate_result == GATE_OK
    assert (report.instructions, report.allowed) == (1, 1)

    decision = full_db_session.query(Decision).one()
    assert decision.gate_result == "OK"
    assert (decision.tokens_in, decision.tokens_out) == (100, 50)

    instruction = full_db_session.query(Instruction).one()
    assert (instruction.action, instruction.symbol, instruction.quantity) == (
        "BUY",
        "NVDA.US",
        10,
    )
    assert instruction.cited_event_ids == ["e1"]
    assert instruction.decision_id == decision.decision_id

    audit = full_db_session.query(RiskAudit).one()
    assert (audit.allow, audit.rule_tripped) == (True, "ALLOWED")

    order = full_db_session.query(Order).one()
    assert order.status == repo.STATUS_FILLED
    assert order.instruction_id == instruction.id
    # client_order_id is derived from the persisted decision identity (§5).
    from tevnnis_core.llm.instruction_unifier import make_client_order_id

    assert order.client_order_id == make_client_order_id(decision.decision_id, 0)


async def test_a_risk_rejection_writes_an_audit_row_and_places_no_order(
    config, full_db_session, now
):
    h = harness(
        config,
        full_db_session,
        responses=[make_response(events=[quote_event()], snapshots=SNAPSHOTS, next_cursor="c1")],
        # 100 x 104 = 10,400, far past max_order_notional (3,000).
        llm_response=buy_output(quantity=100),
    )

    report = await h.loop.run_round(now=now)

    assert report.allowed == 0
    assert [r.rule_id for r in report.rejected] == ["MAX_ORDER_NOTIONAL"]
    audit = full_db_session.query(RiskAudit).one()
    assert (audit.allow, audit.rule_tripped) == (False, "MAX_ORDER_NOTIONAL")
    assert full_db_session.query(Order).count() == 0
    # The instruction is still persisted — the audit trail keeps what was proposed.
    assert full_db_session.query(Instruction).count() == 1


async def test_a_hallucinated_ticker_is_stopped_by_risk_not_by_the_unifier(
    config, full_db_session, now
):
    h = harness(
        config,
        full_db_session,
        responses=[make_response(events=[quote_event()], snapshots=SNAPSHOTS, next_cursor="c1")],
        llm_response=buy_output(symbol="TSLA.US", limit_price=100.0),
    )
    report = await h.loop.run_round(now=now)

    assert [r.rule_id for r in report.rejected] == ["UNIVERSE_ALLOWLIST"]
    assert full_db_session.query(Order).count() == 0


async def test_the_cursor_is_persisted_and_passed_back_on_the_next_pull(
    config, full_db_session, now
):
    h = harness(
        config,
        full_db_session,
        responses=[
            make_response(events=[quote_event("e1", priority="MEDIUM")], next_cursor="c1"),
            make_response(events=[quote_event("e2", priority="MEDIUM")], next_cursor="c2"),
        ],
        llm_response=buy_output(),
    )

    await h.loop.run_round(now=now)
    await h.loop.run_round(now=now)

    assert [r.since_cursor for r in h.md.requests] == ["", "c1"]
    assert repo.load_cursor(full_db_session) == "c2"


async def test_a_restart_resumes_from_the_persisted_cursor(config, full_db_session, now):
    repo.save_cursor(full_db_session, "c9")
    full_db_session.commit()

    h = harness(
        config, full_db_session, responses=[make_response()], llm_response=buy_output()
    )
    await h.loop.run_round(now=now)

    assert h.md.requests[0].since_cursor == "c9"


async def test_the_pull_asks_for_every_priority_so_all_events_are_audited(
    config, full_db_session, now
):
    h = harness(
        config, full_db_session, responses=[make_response()], llm_response=buy_output()
    )
    await h.loop.run_round(now=now)

    request = h.md.requests[0]
    assert request.min_priority == 0  # LOW — the gate, not the pull, decides on waking
    assert set(request.sectors) == set(config.universe)


async def test_a_failing_round_is_rolled_back_and_the_loop_survives(
    config, full_db_session, now
):
    class ExplodingClient:
        async def pull_decision_batch(self, request):
            raise RuntimeError("md went away")

    h = harness(
        config, full_db_session, responses=[make_response()], llm_response=buy_output()
    )
    h.loop.md_client = ExplodingClient()

    report = await h.loop.run_round(now=now)

    assert report.gate_result == "ERROR"
    assert "md went away" in report.error
    assert full_db_session.query(Decision).count() == 0


async def test_two_buys_that_jointly_breach_cash_reject_only_the_second(
    config, full_db_session, now
):
    """§11 sequencing, end to end: one order goes out, the other is stopped."""
    output = DecisionOutput(
        instructions=[
            ProposedInstruction(
                action=Action.BUY, symbol="NVDA.US", quantity=25, limit_price=104.0,
                valid_seconds=300, confidence=0.8, thesis="a", cited_event_ids=["e1"],
            ),
            ProposedInstruction(
                action=Action.BUY, symbol="AMD.US", quantity=20, limit_price=50.0,
                valid_seconds=300, confidence=0.7, thesis="b", cited_event_ids=["e1"],
            ),
        ],
        session_note="two entries",
    )
    h = harness(
        config,
        full_db_session,
        responses=[make_response(events=[quote_event()], snapshots=SNAPSHOTS, next_cursor="c1")],
        llm_response=output,
        cash=2_800.0,
    )

    report = await h.loop.run_round(now=now)

    assert report.allowed == 1
    assert len(report.rejected) == 1
    assert full_db_session.query(Order).count() == 1
    assert full_db_session.query(RiskAudit).count() == 2
