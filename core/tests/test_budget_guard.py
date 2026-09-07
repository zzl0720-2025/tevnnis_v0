"""Tests for the budget guard (§10 app-side soft gate)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from tevnnis_core.config import BudgetsConfig
from tevnnis_core.db.models import ApiUsage
from tevnnis_core.llm.budget import check_budget, record_usage, usage_cost
from tevnnis_core.llm.router import LLMRouter
from tevnnis_core.mocks.llm import MockLLM
from tevnnis_core.mocks.llm_scenarios import hold_scenario

TZ = "America/New_York"

BUDGETS = BudgetsConfig(
    llm_daily_token_budget=1000,
    llm_daily_call_cap=3,
    broker_max_trades_per_day=10,
    broker_max_turnover_per_day=5000,
)

NOW = datetime(2026, 6, 15, 15, 0, tzinfo=timezone.utc)  # 11:00 ET


def test_no_usage_yet_is_ok(db_session):
    status = check_budget(db_session, BUDGETS, now=NOW, tz=TZ)
    assert status.ok
    assert status.tokens_used_today == 0
    assert status.calls_used_today == 0


def test_record_usage_is_reflected_in_next_check(db_session):
    record_usage(db_session, provider="mock", tokens_in=400, tokens_out=100, now=NOW)
    db_session.flush()

    status = check_budget(db_session, BUDGETS, now=NOW, tz=TZ)
    assert status.ok
    assert status.tokens_used_today == 500
    assert status.calls_used_today == 1


def test_token_budget_trips_at_cap(db_session):
    record_usage(db_session, provider="mock", tokens_in=900, tokens_out=100, now=NOW)  # =1000
    db_session.flush()

    status = check_budget(db_session, BUDGETS, now=NOW, tz=TZ)
    assert not status.ok
    assert "token" in status.reason


def test_call_cap_trips_before_token_cap(db_session):
    for _ in range(3):
        record_usage(db_session, provider="mock", tokens_in=10, tokens_out=10, now=NOW)
    db_session.flush()

    status = check_budget(db_session, BUDGETS, now=NOW, tz=TZ)
    assert not status.ok
    assert "call" in status.reason


def test_yesterdays_usage_does_not_count_toward_today(db_session):
    yesterday = NOW - timedelta(days=1)
    db_session.add(
        ApiUsage(
            ts=yesterday,
            kind="llm",
            provider="mock",
            tokens_in=900,
            tokens_out=900,
            cost=0.0,
            call_count=3,
        )
    )
    db_session.flush()

    status = check_budget(db_session, BUDGETS, now=NOW, tz=TZ)
    assert status.ok
    assert status.tokens_used_today == 0
    assert status.calls_used_today == 0


def test_broker_usage_is_not_counted_against_llm_budget(db_session):
    db_session.add(
        ApiUsage(
            kind="broker",
            provider="longbridge",
            tokens_in=None,
            tokens_out=None,
            cost=1.0,
            call_count=1,
        )
    )
    db_session.flush()

    status = check_budget(db_session, BUDGETS, now=NOW, tz=TZ)
    assert status.ok
    assert status.calls_used_today == 0


# ---------------------------------------------------------------------------
# Real provider cost on the §7 ledger.
# ---------------------------------------------------------------------------


def test_usage_cost_is_zero_when_the_route_carries_no_pricing():
    """§7's `cost` column holds a real figure or nothing — never a guess."""
    assert usage_cost(4000, 500, price_in_per_mtok=None, price_out_per_mtok=None) == 0.0


def test_usage_cost_is_per_million_tokens():
    # 4000 in at $0.05/Mtok = $0.0002; 500 out at $0.40/Mtok = $0.0002.
    cost = usage_cost(4000, 500, price_in_per_mtok=0.05, price_out_per_mtok=0.40)
    assert cost == pytest.approx(0.0004)


def test_a_half_priced_route_still_charges_the_side_it_knows():
    assert usage_cost(
        1_000_000, 1_000_000, price_in_per_mtok=2.0, price_out_per_mtok=None
    ) == pytest.approx(2.0)


async def test_a_priced_router_writes_a_real_cost_to_api_usage(db_session, config):
    router = LLMRouter(
        provider=MockLLM(response=hold_scenario()),
        budgets=config.budgets,
        tz=config.cadence.timezone,
        provider_name="openai",
        model_name="gpt-5-nano",
        price_in_per_mtok=1.0,
        price_out_per_mtok=2.0,
    )
    await router.decide(db_session, [], now=NOW)
    db_session.commit()

    row = db_session.execute(select(ApiUsage)).scalars().one()
    expected = usage_cost(
        row.tokens_in, row.tokens_out, price_in_per_mtok=1.0, price_out_per_mtok=2.0
    )
    assert row.cost == pytest.approx(expected)
    assert row.cost > 0


async def test_an_unpriced_router_leaves_cost_at_zero(db_session, config):
    """The pre-Stage-8 behaviour, kept: no pricing, no invented figure."""
    router = LLMRouter(
        provider=MockLLM(response=hold_scenario()),
        budgets=config.budgets,
        tz=config.cadence.timezone,
        provider_name="mock",
    )
    await router.decide(db_session, [], now=NOW)
    db_session.commit()

    assert db_session.execute(select(ApiUsage)).scalars().one().cost == 0.0
