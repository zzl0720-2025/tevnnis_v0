"""Canned DecisionOutput scenarios for MockLLM — reusable across tests.

Each builder returns a fresh, deterministic DecisionOutput so scenarios can be
selected by name without sharing mutable state between tests.
"""

from __future__ import annotations

from tevnnis_core.instructions import Action, DecisionOutput, OrderType, ProposedInstruction


def hold_scenario(note: str = "no setup meets the bar") -> DecisionOutput:
    return DecisionOutput(instructions=[], session_note=note)


def buy_scenario(
    symbol: str = "AAPL.US",
    quantity: int = 10,
    limit_price: float = 150.0,
    confidence: float = 0.8,
    valid_seconds: int = 300,
    cited_event_ids: list[str] | None = None,
) -> DecisionOutput:
    return DecisionOutput(
        instructions=[
            ProposedInstruction(
                action=Action.BUY,
                symbol=symbol,
                order_type=OrderType.LIMIT,
                quantity=quantity,
                limit_price=limit_price,
                valid_seconds=valid_seconds,
                confidence=confidence,
                thesis="scripted buy scenario",
                cited_event_ids=cited_event_ids or [],
            )
        ],
        session_note="buy scenario",
    )


def sell_scenario(
    symbol: str = "AAPL.US",
    quantity: int = 10,
    limit_price: float = 155.0,
    confidence: float = 0.8,
    valid_seconds: int = 300,
    cited_event_ids: list[str] | None = None,
) -> DecisionOutput:
    return DecisionOutput(
        instructions=[
            ProposedInstruction(
                action=Action.SELL,
                symbol=symbol,
                order_type=OrderType.LIMIT,
                quantity=quantity,
                limit_price=limit_price,
                valid_seconds=valid_seconds,
                confidence=confidence,
                thesis="scripted sell scenario",
                cited_event_ids=cited_event_ids or [],
            )
        ],
        session_note="sell scenario",
    )
