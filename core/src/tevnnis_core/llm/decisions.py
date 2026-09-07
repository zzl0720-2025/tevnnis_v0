"""Decision-identity persistence (§5, §7, §8).

`client_order_id` must be a deterministic hash of a STABLE, PERSISTED decision
identity (decision_id + instruction_index), so a retry of the same decision
round reproduces the same order ids instead of double-submitting. That only
holds if decision_id is written to `decisions` *before* the LLM is spent, and
the write is idempotent so a crash-and-retry with the same id is a no-op.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from tevnnis_core.db.models import Decision


def new_decision_id() -> str:
    return str(uuid4())


def persist_pending_decision(session: Session, decision_id: str) -> None:
    """Insert a PENDING decisions row if one doesn't already exist (idempotent)."""
    if session.get(Decision, decision_id) is not None:
        return
    session.add(Decision(decision_id=decision_id, gate_result="PENDING"))
    session.flush()


def finalize_decision(
    session: Session,
    decision_id: str,
    *,
    gate_result: str,
    model_used: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    session_note: str | None = None,
) -> None:
    """Update a previously-persisted decision with its outcome."""
    decision = session.get(Decision, decision_id)
    if decision is None:
        raise ValueError(f"finalize_decision: no PENDING decision {decision_id!r} to finalize")
    decision.gate_result = gate_result
    decision.model_used = model_used
    decision.tokens_in = tokens_in
    decision.tokens_out = tokens_out
    decision.session_note = session_note
