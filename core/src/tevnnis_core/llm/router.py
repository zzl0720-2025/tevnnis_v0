"""LLM Router — provider abstraction + budget guard (§2.1, §10).

Wraps one LLMProvider (v0's "strong" tier — §10 defers cheap/local triage to
v0.x). Every decision round gets a stable, persisted decision_id *before* any
provider call, so a crash-and-retry with the same id is safe (llm/decisions.py).
The budget guard runs first: if the daily token/call cap is already spent,
the provider is never called and the round is logged as a HOLD. A provider
call that raises (malformed/wrong-shape output — e.g. it fails to parse into
DecisionOutput) is also caught and logged as a HOLD rather than propagating —
the LLM proposes; a bad proposal must never crash the loop.

The caller owns the session's transaction boundary (commit/rollback); this
module only adds/flushes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from tevnnis_core.config import BudgetsConfig
from tevnnis_core.instructions import DecisionOutput
from tevnnis_core.llm.budget import check_budget, record_usage, usage_cost
from tevnnis_core.llm.decisions import finalize_decision, new_decision_id, persist_pending_decision
from tevnnis_core.ports import LLMProvider

GATE_OK = "OK"
GATE_HOLD_BUDGET = "HOLD_BUDGET"
GATE_HOLD_MALFORMED_OUTPUT = "HOLD_MALFORMED_OUTPUT"


@dataclass(frozen=True)
class RouterOutcome:
    decision_id: str
    status: str  # "ok" | "hold_budget" | "hold_malformed_output"
    output: DecisionOutput | None
    tokens_in: int | None = None
    tokens_out: int | None = None
    reason: str = ""


@dataclass
class LLMRouter:
    """Routes one decision round to the strong-tier provider (§10).

    `provider_name` and `model_name` are two different facts, kept apart:
    `provider_name` ("mock" / "openai") is who was billed and lands in
    `api_usage.provider`, while `model_name` ("gpt-5-nano") is what actually
    reasoned and lands in `decisions.model_used`. Keeping these fields separate
    prevents a broker identifier from being recorded as the LLM model.
    """

    provider: LLMProvider
    budgets: BudgetsConfig
    tz: str
    provider_name: str = "mock"
    model_name: str | None = None
    # USD per 1M tokens for this route, from §6 llm_routing. None on either
    # side means the route is unpriced and every api_usage row keeps cost 0.0.
    price_in_per_mtok: float | None = None
    price_out_per_mtok: float | None = None

    def _cost(self, tokens_in: int, tokens_out: int) -> float:
        return usage_cost(
            tokens_in,
            tokens_out,
            price_in_per_mtok=self.price_in_per_mtok,
            price_out_per_mtok=self.price_out_per_mtok,
        )

    @property
    def model_used(self) -> str:
        """What to record in `decisions.model_used` — the model where known."""
        return self.model_name or self.provider_name

    async def decide(
        self,
        session: Session,
        messages: list[Any],
        *,
        decision_id: str | None = None,
        now: datetime | None = None,
    ) -> RouterOutcome:
        decision_id = decision_id or new_decision_id()
        now = now or datetime.now(timezone.utc)

        persist_pending_decision(session, decision_id)

        status = check_budget(session, self.budgets, now=now, tz=self.tz)
        if not status.ok:
            finalize_decision(
                session, decision_id, gate_result=GATE_HOLD_BUDGET, session_note=status.reason
            )
            return RouterOutcome(decision_id, "hold_budget", output=None, reason=status.reason)

        try:
            output = await self.provider.complete_structured(messages, DecisionOutput)
        except Exception as exc:  # noqa: BLE001 — any provider/parse failure degrades to HOLD
            reason = f"{type(exc).__name__}: {exc}"
            # A FAILED CALL CAN STILL HAVE COST MONEY. With a real provider the
            # request may reach the API, burn tokens, and only then fail to
            # parse. `last_usage()` means "the usage of the call just attempted,
            # or None if it spent nothing", so a non-None reading here is spend
            # that must hit the ledger — otherwise a run of malformed responses
            # would bill real money without ever moving the daily cap, and the
            # guard would be a guard in name only (§10).
            spent = self.provider.last_usage()
            tokens_in = spent.tokens_in if spent else None
            tokens_out = spent.tokens_out if spent else None
            if spent is not None:
                record_usage(
                    session,
                    provider=self.provider_name,
                    tokens_in=spent.tokens_in,
                    tokens_out=spent.tokens_out,
                    cost=self._cost(spent.tokens_in, spent.tokens_out),
                    now=now,
                )
            finalize_decision(
                session,
                decision_id,
                gate_result=GATE_HOLD_MALFORMED_OUTPUT,
                model_used=self.model_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                session_note=reason,
            )
            return RouterOutcome(
                decision_id,
                "hold_malformed_output",
                output=None,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                reason=reason,
            )

        usage = self.provider.last_usage()
        tokens_in = usage.tokens_in if usage else 0
        tokens_out = usage.tokens_out if usage else 0

        record_usage(
            session,
            provider=self.provider_name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=self._cost(tokens_in, tokens_out),
            now=now,
        )
        finalize_decision(
            session,
            decision_id,
            gate_result=GATE_OK,
            model_used=self.model_used,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            session_note=output.session_note,
        )
        return RouterOutcome(
            decision_id, "ok", output=output, tokens_in=tokens_in, tokens_out=tokens_out
        )
