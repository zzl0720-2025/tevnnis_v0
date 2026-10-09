"""Convert validated model output into canonical trading instructions.

Assign stable client-order IDs and separate thesis/citation fields for storage.
Pydantic validates the input schema; the risk engine checks trade eligibility,
including the BUY-only universe allowlist.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from tevnnis_core.instructions import DecisionOutput, ProposedInstruction, TradingInstruction


@dataclass(frozen=True)
class PersistableInstruction:
    """thesis / cited_event_ids: for the instructions table, never sent to Execution."""

    thesis: str
    cited_event_ids: list[str]


@dataclass(frozen=True)
class UnifiedInstruction:
    trading_instruction: TradingInstruction
    persistable: PersistableInstruction


@dataclass(frozen=True)
class UnifiedDecision:
    instructions: list[UnifiedInstruction]
    session_note: str


def make_client_order_id(decision_id: str, index: int) -> str:
    """Deterministic idempotency key: a pure function of decision_id + index.

    Retrying the same persisted decision reproduces the same ID for deduplication.
    This does not by itself guarantee exactly-once broker submission.
    """
    digest = hashlib.sha256(f"{decision_id}:{index}".encode()).hexdigest()
    return f"co_{digest[:24]}"


def unify(decision_id: str, output: DecisionOutput) -> UnifiedDecision:
    """Build canonical TradingInstruction(s) from an already-validated DecisionOutput."""
    instructions = [
        _build(decision_id, index, proposed) for index, proposed in enumerate(output.instructions)
    ]
    return UnifiedDecision(instructions=instructions, session_note=output.session_note)


def _build(decision_id: str, index: int, proposed: ProposedInstruction) -> UnifiedInstruction:
    trading_instruction = TradingInstruction(
        action=proposed.action,
        symbol=proposed.symbol,
        order_type=proposed.order_type,
        quantity=proposed.quantity,
        limit_price=proposed.limit_price,
        valid_seconds=proposed.valid_seconds,
        confidence=proposed.confidence,
        client_order_id=make_client_order_id(decision_id, index),
    )
    persistable = PersistableInstruction(
        thesis=proposed.thesis,
        cited_event_ids=list(proposed.cited_event_ids),
    )
    return UnifiedInstruction(trading_instruction=trading_instruction, persistable=persistable)
