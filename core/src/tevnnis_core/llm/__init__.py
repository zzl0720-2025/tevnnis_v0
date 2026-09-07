"""The LLM reasoning path (§5, §6, §8, §10): Protocol Unifier, Router, Trading Instruction Unifier.

No decision loop lives here — this package only turns state into a prompt,
turns a prompt into a validated DecisionOutput (with budget enforcement), and
turns that DecisionOutput into canonical, execution-ready instructions.
"""

from __future__ import annotations

from tevnnis_core.llm.budget import BudgetStatus, check_budget, record_usage
from tevnnis_core.llm.decisions import finalize_decision, new_decision_id, persist_pending_decision
from tevnnis_core.llm.instruction_unifier import (
    PersistableInstruction,
    UnifiedDecision,
    UnifiedInstruction,
    make_client_order_id,
    unify,
)
from tevnnis_core.llm.protocol_unifier import (
    AccountSnapshot,
    SectorFact,
    SectorSymbolFact,
    SelectedEvent,
    build_dynamic_state,
    build_prompt_messages,
    build_static_prefix,
)
from tevnnis_core.llm.router import LLMRouter, RouterOutcome

__all__ = [
    "BudgetStatus",
    "check_budget",
    "record_usage",
    "finalize_decision",
    "new_decision_id",
    "persist_pending_decision",
    "PersistableInstruction",
    "UnifiedDecision",
    "UnifiedInstruction",
    "make_client_order_id",
    "unify",
    "AccountSnapshot",
    "SectorFact",
    "SectorSymbolFact",
    "SelectedEvent",
    "build_dynamic_state",
    "build_prompt_messages",
    "build_static_prefix",
    "LLMRouter",
    "RouterOutcome",
]
