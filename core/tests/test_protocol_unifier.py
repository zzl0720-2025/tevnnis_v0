"""Test static strategy input and dynamic state formatting for model prompts."""

from __future__ import annotations

from tevnnis_core.config import StyleConfig
from tevnnis_core.llm.protocol_unifier import (
    SectorFact,
    SectorSymbolFact,
    SelectedEvent,
    build_dynamic_state,
    build_prompt_messages,
    build_static_prefix,
)
from tevnnis_core.ports import AccountSnapshot, PositionSnapshot

STYLE = StyleConfig(
    risk_appetite="moderate",
    holding_bias="long_term",
    notes="Prefer fundamentals; avoid chasing spikes.",
)

# Static prefix


def test_static_prefix_depends_only_on_style():
    a = build_static_prefix(STYLE)
    b = build_static_prefix(STYLE)
    assert a == b


def test_static_prefix_carries_style_fields():
    prefix = build_static_prefix(STYLE)
    assert "moderate" in prefix
    assert "long_term" in prefix
    assert "Prefer fundamentals" in prefix


def test_static_prefix_never_mentions_risk_limits_or_budgets():
    prefix = build_static_prefix(STYLE)
    # risk_limits/budgets are invisible to the LLM by design.
    for forbidden in ("max_position_pct", "llm_daily_token_budget", "max_order_notional"):
        assert forbidden not in prefix


def test_static_prefix_varies_with_style():
    other = StyleConfig(risk_appetite="aggressive", holding_bias="short_term", notes="")
    assert build_static_prefix(STYLE) != build_static_prefix(other)


# Dynamic state


def test_dynamic_state_empty_inputs_render_placeholders():
    state = build_dynamic_state(events=[], sectors=[], positions=[])
    assert "(none held)" in state
    assert "(none)" in state


def test_dynamic_state_never_contains_url_or_body():
    events = [
        SelectedEvent(
            event_id="evt-1",
            type="NEWS",
            symbol="AAPL.US",
            sector="Web",
            priority="MEDIUM",
            summary="Apple announces record quarter",
        )
    ]
    state = build_dynamic_state(events=events, sectors=[], positions=[])
    assert "http" not in state
    assert "url" not in state.lower()


def test_dynamic_state_computes_unrealized_pnl_in_code():
    positions = [PositionSnapshot(symbol="AAPL.US", quantity=10, cost_basis=100.0)]
    sectors = [
        SectorFact(
            sector="Web",
            symbols=[SectorSymbolFact(symbol="AAPL.US", last_price=110.0, change_pct=2.0)],
        )
    ]
    state = build_dynamic_state(events=[], sectors=sectors, positions=positions)
    assert "pnl_pct=+10.00%" in state
    assert "last=110.00" in state


def test_dynamic_state_position_without_a_quote_is_marked_unavailable():
    positions = [PositionSnapshot(symbol="ZZZ.US", quantity=1, cost_basis=50.0)]
    state = build_dynamic_state(events=[], sectors=[], positions=positions)
    assert "pnl_pct=n/a" in state


def test_dynamic_state_includes_account_facts():
    account = AccountSnapshot(buying_power=1800.0, cash=2000.0, net_liquidation=12000.0)
    state = build_dynamic_state(events=[], sectors=[], positions=[], account=account)
    assert "cash=2000.00" in state
    assert "buying_power=1800.00" in state


def test_dynamic_state_is_not_raw_json():
    events = [
        SelectedEvent(
            event_id="evt-1",
            type="QUOTE_MOVE",
            symbol="NVDA.US",
            sector="Semiconductor",
            priority="HIGH",
            summary="cross_+3pct",
            change_pct=4.1,
        )
    ]
    state = build_dynamic_state(events=events, sectors=[], positions=[])
    assert not state.strip().startswith("{")
    assert '"event_id"' not in state


# Assembled prompt


def test_build_prompt_messages_splits_static_and_dynamic():
    messages = build_prompt_messages(STYLE, events=[], sectors=[], positions=[])
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[0]["content"] == build_static_prefix(STYLE)
