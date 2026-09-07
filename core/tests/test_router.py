"""Tests for the LLM Router (§2.1, §10 — provider abstraction + budget guard)."""

from __future__ import annotations

from datetime import datetime, timezone

from tevnnis_core.config import BudgetsConfig
from tevnnis_core.db.models import Decision
from tevnnis_core.llm.router import GATE_HOLD_BUDGET, GATE_HOLD_MALFORMED_OUTPUT, GATE_OK, LLMRouter
from tevnnis_core.mocks.llm import MockLLM
from tevnnis_core.mocks.llm_scenarios import buy_scenario, hold_scenario

TZ = "America/New_York"
NOW = datetime(2026, 6, 15, 15, 0, tzinfo=timezone.utc)

ROOMY_BUDGETS = BudgetsConfig(
    llm_daily_token_budget=1_000_000,
    llm_daily_call_cap=50,
    broker_max_trades_per_day=10,
    broker_max_turnover_per_day=5000,
)

TIGHT_BUDGETS = BudgetsConfig(
    llm_daily_token_budget=1_000_000,
    llm_daily_call_cap=1,
    broker_max_trades_per_day=10,
    broker_max_turnover_per_day=5000,
)


async def test_successful_call_returns_ok_and_persists_decision(db_session):
    llm = MockLLM(response=buy_scenario(symbol="NVDA.US"), tokens_in=4000, tokens_out=500)
    router = LLMRouter(provider=llm, budgets=ROOMY_BUDGETS, tz=TZ)

    outcome = await router.decide(db_session, messages=[], now=NOW)

    assert outcome.status == "ok"
    assert outcome.output.instructions[0].symbol == "NVDA.US"
    assert outcome.tokens_in == 4000
    assert outcome.tokens_out == 500
    assert llm.call_count == 1

    decision = db_session.get(Decision, outcome.decision_id)
    assert decision.gate_result == GATE_OK
    assert decision.tokens_in == 4000
    assert decision.tokens_out == 500
    assert decision.model_used == "mock"


async def test_decision_id_is_persisted_before_the_provider_is_called(db_session):
    llm = MockLLM(response=hold_scenario())
    router = LLMRouter(provider=llm, budgets=ROOMY_BUDGETS, tz=TZ)

    outcome = await router.decide(db_session, messages=[], now=NOW)

    # decision_id is core-generated, not something the caller had to supply.
    assert outcome.decision_id
    assert db_session.get(Decision, outcome.decision_id) is not None


async def test_reusing_a_decision_id_does_not_duplicate_the_row(db_session):
    llm = MockLLM(response=hold_scenario())
    router = LLMRouter(provider=llm, budgets=ROOMY_BUDGETS, tz=TZ)

    first = await router.decide(db_session, messages=[], decision_id="fixed-id", now=NOW)
    second = await router.decide(db_session, messages=[], decision_id="fixed-id", now=NOW)

    assert first.decision_id == second.decision_id == "fixed-id"
    count = db_session.query(Decision).filter_by(decision_id="fixed-id").count()
    assert count == 1


async def test_budget_exhausted_refuses_and_never_calls_the_provider(db_session):
    llm = MockLLM(response=buy_scenario())
    router = LLMRouter(provider=llm, budgets=TIGHT_BUDGETS, tz=TZ)

    # Spend the single allowed call first.
    await router.decide(db_session, messages=[], now=NOW)
    assert llm.call_count == 1

    outcome = await router.decide(db_session, messages=[], now=NOW)

    assert outcome.status == "hold_budget"
    assert outcome.output is None
    assert llm.call_count == 1  # provider was NOT called a second time

    decision = db_session.get(Decision, outcome.decision_id)
    assert decision.gate_result == GATE_HOLD_BUDGET


async def test_malformed_provider_output_is_rejected_cleanly_as_hold(db_session):
    # MockLLM raises TypeError when its scenario doesn't match response_model —
    # this stands in for a real provider whose structured-output parse fails.
    llm = MockLLM(response="not a DecisionOutput")
    router = LLMRouter(provider=llm, budgets=ROOMY_BUDGETS, tz=TZ)

    outcome = await router.decide(db_session, messages=[], now=NOW)

    assert outcome.status == "hold_malformed_output"
    assert outcome.output is None

    decision = db_session.get(Decision, outcome.decision_id)
    assert decision.gate_result == GATE_HOLD_MALFORMED_OUTPUT
