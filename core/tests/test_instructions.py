"""Tests for ProposedInstruction validation (§5 — Validation ≠ Risk)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tevnnis_core.instructions import Action, DecisionOutput, ProposedInstruction

# ---------------------------------------------------------------------------
# HOLD
# ---------------------------------------------------------------------------


def test_hold_valid():
    inst = ProposedInstruction(action=Action.HOLD, symbol="AAPL.US", confidence=0.9)
    assert inst.action == Action.HOLD
    assert inst.quantity == 0
    assert inst.limit_price == 0.0


def test_hold_with_quantity_rejected():
    with pytest.raises(ValidationError, match="HOLD"):
        ProposedInstruction(action=Action.HOLD, symbol="AAPL.US", confidence=0.5, quantity=100)


def test_hold_with_limit_price_rejected():
    with pytest.raises(ValidationError, match="HOLD"):
        ProposedInstruction(action=Action.HOLD, symbol="AAPL.US", confidence=0.5, limit_price=150.0)


def test_hold_carries_thesis_and_cited_ids():
    inst = ProposedInstruction(
        action=Action.HOLD,
        symbol="AAPL.US",
        confidence=0.8,
        thesis="Watching for breakout.",
        cited_event_ids=["evt-001"],
    )
    assert inst.thesis == "Watching for breakout."
    assert inst.cited_event_ids == ["evt-001"]


# ---------------------------------------------------------------------------
# BUY
# ---------------------------------------------------------------------------


def test_buy_valid():
    inst = ProposedInstruction(
        action=Action.BUY,
        symbol="AAPL.US",
        quantity=10,
        limit_price=150.0,
        confidence=0.8,
    )
    assert inst.quantity == 10
    assert inst.limit_price == 150.0


def test_buy_without_quantity_rejected():
    with pytest.raises(ValidationError, match="quantity"):
        ProposedInstruction(
            action=Action.BUY, symbol="AAPL.US", limit_price=150.0, confidence=0.8
        )


def test_buy_zero_quantity_rejected():
    with pytest.raises(ValidationError, match="quantity"):
        ProposedInstruction(
            action=Action.BUY, symbol="AAPL.US", quantity=0, limit_price=150.0, confidence=0.8
        )


def test_buy_without_price_rejected():
    with pytest.raises(ValidationError, match="limit_price"):
        ProposedInstruction(action=Action.BUY, symbol="AAPL.US", quantity=10, confidence=0.8)


def test_buy_negative_price_rejected():
    with pytest.raises(ValidationError, match="limit_price"):
        ProposedInstruction(
            action=Action.BUY, symbol="AAPL.US", quantity=10, limit_price=-1.0, confidence=0.8
        )


# ---------------------------------------------------------------------------
# SELL
# ---------------------------------------------------------------------------


def test_sell_valid():
    inst = ProposedInstruction(
        action=Action.SELL,
        symbol="NVDA.US",
        quantity=5,
        limit_price=800.0,
        confidence=0.7,
    )
    assert inst.action == Action.SELL


def test_sell_without_quantity_rejected():
    with pytest.raises(ValidationError, match="quantity"):
        ProposedInstruction(
            action=Action.SELL, symbol="NVDA.US", limit_price=800.0, confidence=0.7
        )


def test_sell_without_price_rejected():
    with pytest.raises(ValidationError, match="limit_price"):
        ProposedInstruction(action=Action.SELL, symbol="NVDA.US", quantity=5, confidence=0.7)


# ---------------------------------------------------------------------------
# confidence range
# ---------------------------------------------------------------------------


def test_confidence_above_one_rejected():
    with pytest.raises(ValidationError):
        ProposedInstruction(action=Action.HOLD, symbol="AAPL.US", confidence=1.1)


def test_confidence_below_zero_rejected():
    with pytest.raises(ValidationError):
        ProposedInstruction(action=Action.HOLD, symbol="AAPL.US", confidence=-0.1)


def test_confidence_boundary_values_accepted():
    assert ProposedInstruction(action=Action.HOLD, symbol="X.US", confidence=0.0).confidence == 0.0
    assert ProposedInstruction(action=Action.HOLD, symbol="X.US", confidence=1.0).confidence == 1.0


# ---------------------------------------------------------------------------
# Extra fields forbidden
# ---------------------------------------------------------------------------


def test_extra_field_on_proposed_instruction_rejected():
    with pytest.raises(ValidationError):
        ProposedInstruction(
            action=Action.HOLD,
            symbol="AAPL.US",
            confidence=0.5,
            unexpected="value",
        )


# ---------------------------------------------------------------------------
# DecisionOutput
# ---------------------------------------------------------------------------


def test_decision_output_empty():
    out = DecisionOutput()
    assert out.instructions == []
    assert out.session_note == ""


def test_decision_output_accepts_up_to_five_instructions():
    out = DecisionOutput(
        instructions=[
            ProposedInstruction(action=Action.HOLD, symbol=f"S{i}.US", confidence=0.5)
            for i in range(5)
        ]
    )
    assert len(out.instructions) == 5


def test_decision_output_rejects_more_than_five_instructions():
    with pytest.raises(ValidationError):
        DecisionOutput(
            instructions=[
                ProposedInstruction(action=Action.HOLD, symbol=f"S{i}.US", confidence=0.5)
                for i in range(6)
            ]
        )


def test_decision_output_with_instructions():
    out = DecisionOutput(
        instructions=[
            ProposedInstruction(action=Action.HOLD, symbol="AAPL.US", confidence=0.9),
            ProposedInstruction(
                action=Action.BUY, symbol="NVDA.US", quantity=2, limit_price=900.0, confidence=0.75
            ),
        ],
        session_note="Mixed signals — holding most, small NVDA entry.",
    )
    assert len(out.instructions) == 2
    assert out.session_note.startswith("Mixed")
