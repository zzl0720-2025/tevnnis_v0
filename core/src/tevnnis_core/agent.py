"""The §8 decision loop — one round, start to finish.

    pull -> persist events -> tick the order lifecycle -> cheap gate
         -> assemble state -> LLM router -> unify
         -> persist instructions -> Risk (sequential batch) -> persist audit
         -> Execution -> commit

Two invariants the rest of the system leans on:

**HOLD is always logged.** Every exit from this round — kill switch, spent
budget, an empty or unremarkable batch, a malformed model response, an
all-rejected risk batch — leaves a `decisions` row behind. Silence is never a
valid outcome (§7, §8 step 3).

**One round is one transaction.** Everything a round persists commits together
or not at all, so a crash mid-round cannot leave an order row without its
instruction, or a fill without its order. On an unexpected error the round is
rolled back and the loop keeps running; the deterministic `client_order_id`
(§5) is what makes retrying that decision safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from tevnnis_core.config import StrategyConfig
from tevnnis_core.db import repository as repo
from tevnnis_core.execution import ExecutionEngine, SubmitResult
from tevnnis_core.instructions import Action
from tevnnis_core.llm.budget import BudgetStatus, check_budget
from tevnnis_core.llm.decisions import finalize_decision, new_decision_id, persist_pending_decision
from tevnnis_core.llm.instruction_unifier import unify
from tevnnis_core.llm.protocol_unifier import build_prompt_messages
from tevnnis_core.llm.router import LLMRouter
from tevnnis_core.market_data import (
    PulledBatch,
    build_pull_request,
    map_pull_response,
    priority_rank,
)
from tevnnis_core.ports import MarketDataClient, TradePort
from tevnnis_core.risk_context import (
    RiskActivity,
    build_risk_context,
    check_batch,
    load_risk_module,
)

#: How many events one pull may return. Not a §6 knob — md's four throttling
#: knobs (§4.4) already govern how much reaches us; this is only a ceiling on
#: how much of a backlog a single prompt has to carry.
MAX_EVENTS_PER_PULL = 20

#: core pulls every priority so all events are persisted for audit and the
#: sector snapshots stay current; the cheap gate, not the pull, is what decides
#: whether the strong model wakes (§6 `cadence.reason_min_priority`).
PULL_MIN_PRIORITY = "LOW"

GATE_OK = "OK"
GATE_HOLD_KILL_SWITCH = "HOLD_KILL_SWITCH"
GATE_HOLD_BUDGET = "HOLD_BUDGET"
GATE_HOLD_NO_EVENTS = "HOLD_NO_EVENTS"
GATE_HOLD_LOW_PRIORITY = "HOLD_LOW_PRIORITY"


@dataclass(frozen=True)
class GateOutcome:
    """Why a round did or did not wake the strong model.

    TWO REASONS, deliberately. `reason` is operator-facing: it goes to the
    terminal, where naming the concrete threshold is exactly what the operator
    wants. `public_reason` is what gets PERSISTED to `decisions.session_note`,
    and a gated HOLD has no instruction row, so that note becomes the `thesis`
    the public snapshot publishes (§7/§14, tevnnis_core/snapshot.py).

    So `public_reason` must never quote a §6 configuration value. It defaults to
    `reason`, because most gate reasons carry no configuration at all — only the
    one that compares against `cadence.reason_min_priority` needs to differ.
    Getting the value out HERE, at the source, is the real fix; the scrubber in
    snapshot.py covers the same ground as defence in depth rather than as the
    only line of defence.
    """

    proceed: bool
    result: str
    reason: str = ""
    #: Publishable form of `reason`. Defaults to it; override to strip config.
    public_reason: str | None = None

    @property
    def published_reason(self) -> str:
        return self.reason if self.public_reason is None else self.public_reason


def cheap_gate(
    batch: PulledBatch,
    *,
    budget: BudgetStatus,
    reason_min_priority: str,
    kill_switch: bool = False,
) -> GateOutcome:
    """§8 step 3 — is this batch worth waking the strong model?

    Pure, and checked in cost order: the reasons that spend nothing come first.
    The budget is re-checked inside the Router too; testing it here means a
    spent budget does not even build a prompt.
    """
    if kill_switch:
        return GateOutcome(False, GATE_HOLD_KILL_SWITCH, "kill switch engaged")
    if not budget.ok:
        return GateOutcome(False, GATE_HOLD_BUDGET, budget.reason)
    if not batch.records:
        return GateOutcome(False, GATE_HOLD_NO_EVENTS, "no new events in this batch")

    highest = batch.max_priority or "LOW"
    if priority_rank(highest) < priority_rank(reason_min_priority):
        # The observed batch priority is an observation and is publishable; the
        # threshold it was compared against is §6 configuration and is not. The
        # operator sees both, the world sees only the first.
        return GateOutcome(
            False,
            GATE_HOLD_LOW_PRIORITY,
            f"highest priority in batch is {highest}, below reason_min_priority="
            f"{reason_min_priority}",
            public_reason=f"highest priority in batch is {highest}, "
            "below the wake threshold",
        )
    return GateOutcome(True, GATE_OK)


@dataclass
class RejectedInstruction:
    symbol: str
    action: str
    rule_id: str
    reason: str


@dataclass
class RoundReport:
    """Everything the CLI needs to explain one round to the operator."""

    gate_result: str
    decision_id: str | None = None
    reason: str = ""
    events_pulled: int = 0
    events_new: int = 0
    dropped_count: int = 0
    model_used: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    instructions: int = 0
    allowed: int = 0
    rejected: list[RejectedInstruction] = field(default_factory=list)
    submissions: list[SubmitResult] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def is_hold(self) -> bool:
        return self.gate_result != GATE_OK or self.instructions == 0


@dataclass
class AgentLoop:
    """One decision round per `run_round()` call; the CLI owns the cadence."""

    config: StrategyConfig
    session: Session
    md_client: MarketDataClient
    trade_port: TradePort
    router: LLMRouter
    execution: ExecutionEngine
    risk_module: Any | None = None

    def __post_init__(self) -> None:
        self.cursor: str = repo.load_cursor(self.session)
        self.symbol_status: dict[str, str] = {}
        self.last_prices: dict[str, float] = {}
        #: Latest change_pct per symbol, from md's sector snapshots. Carried
        #: alongside last_prices purely so the CLI can sample both into
        #: price_samples for the public snapshot; the decision path
        #: does not read it.
        self.last_change_pct: dict[str, float] = {}
        self.kill_switch: bool = False
        self.rounds: int = 0
        self.holds: int = 0

    @property
    def _risk(self) -> Any:
        if self.risk_module is None:
            self.risk_module = load_risk_module()
        return self.risk_module

    # -- one round -----------------------------------------------------------

    async def run_round(self, *, now: datetime) -> RoundReport:
        self.rounds += 1
        try:
            report = await self._run_round(now=now)
            self.session.commit()
        except Exception as exc:  # noqa: BLE001 — one bad round must not end the session
            self.session.rollback()
            report = RoundReport(
                gate_result="ERROR", error=f"{type(exc).__name__}: {exc}", reason=str(exc)
            )
        if report.is_hold:
            self.holds += 1
        return report

    async def _run_round(self, *, now: datetime) -> RoundReport:
        tz = self.config.cadence.timezone

        # 1-2. Pull, persist events, advance the cursor (§4.3, §8 steps 1-2).
        batch = await self._pull(now=now)
        events_new = repo.persist_events(self.session, batch.records)

        # 3. Tick the order lifecycle before deciding, so the state we reason
        #    over (and risk-check against) includes anything that just filled.
        await self.execution.poll_updates(now=now)
        expired = await self.execution.expire_timed_orders(now=now)

        report = RoundReport(
            gate_result=GATE_OK,
            events_pulled=len(batch.records),
            events_new=events_new,
            dropped_count=batch.dropped_count,
            expired=expired,
        )

        # 4. Cheap gate — the first LLM-cost gate core itself controls (§8 step 3).
        budget = check_budget(self.session, self.config.budgets, now=now, tz=tz)
        gate = cheap_gate(
            batch,
            budget=budget,
            reason_min_priority=self.config.cadence.reason_min_priority,
            kill_switch=self.kill_switch,
        )
        if not gate.proceed:
            report.gate_result = gate.result
            report.reason = gate.reason
            report.decision_id = self._log_hold(gate.result, gate.published_reason)
            return report

        # 5. Assemble state: market facts from md, position facts from the broker.
        account = await self.trade_port.query_account()
        positions = await self.trade_port.query_positions()
        messages = build_prompt_messages(
            self.config.style, batch.selected, batch.sectors, positions, account
        )

        # 6. Reasoning path — budget guard, provider call, persistence.
        outcome = await self.router.decide(self.session, messages, now=now)
        report.decision_id = outcome.decision_id
        report.tokens_in = outcome.tokens_in or 0
        report.tokens_out = outcome.tokens_out or 0
        report.model_used = self.router.provider_name
        if outcome.status != "ok" or outcome.output is None:
            report.gate_result = (
                GATE_HOLD_BUDGET if outcome.status == "hold_budget" else "HOLD_MALFORMED_OUTPUT"
            )
            report.reason = outcome.reason
            return report

        # 7. Canonical instructions + persistence (§5: thesis/cited ids split off).
        unified = unify(outcome.decision_id, outcome.output)
        report.instructions = len(unified.instructions)
        if not unified.instructions:
            report.reason = outcome.output.session_note
            return report
        instruction_ids = repo.persist_instructions(self.session, outcome.decision_id, unified)

        # 8. Risk first, always (§8 step 7). The engine walks provisional state.
        verdicts = self._risk_check(positions, account, now=now, unified=unified)
        allowed: list[tuple[Any, int]] = []
        for item, instruction_id, verdict in zip(
            unified.instructions, instruction_ids, verdicts
        ):
            repo.persist_risk_audit(
                self.session,
                instruction_id,
                allow=verdict.allowed,
                rule_id=verdict.rule_id,
                ts=now,
            )
            instruction = item.trading_instruction
            if verdict.allowed:
                if instruction.action != Action.HOLD:
                    allowed.append((instruction, instruction_id))
            else:
                report.rejected.append(
                    RejectedInstruction(
                        symbol=instruction.symbol,
                        action=instruction.action.name,
                        rule_id=verdict.rule_id,
                        reason=verdict.reason,
                    )
                )
        report.allowed = len(allowed)

        # 9. Execution (§8 step 8).
        if allowed:
            report.submissions = await self.execution.submit_batch(allowed, now=now)
        return report

    # -- helpers -------------------------------------------------------------

    async def _pull(self, *, now: datetime) -> PulledBatch:
        request = build_pull_request(
            max_events=MAX_EVENTS_PER_PULL,
            min_priority=PULL_MIN_PRIORITY,
            sectors=list(self.config.universe),
            since_cursor=self.cursor,
        )
        response = await self.md_client.pull_decision_batch(request)
        batch = map_pull_response(response)

        if batch.next_cursor and batch.next_cursor != self.cursor:
            self.cursor = batch.next_cursor
            repo.save_cursor(self.session, self.cursor, ts=now)
        self.symbol_status.update(batch.status_updates)
        self.last_prices.update(batch.last_prices)
        for sector in batch.sectors:
            for symbol_fact in sector.symbols:
                self.last_change_pct[symbol_fact.symbol] = symbol_fact.change_pct
        return batch

    def _log_hold(self, gate_result: str, reason: str) -> str:
        """Persist a HOLD decision — no LLM call, no order, but never silence.

        `reason` must be a GateOutcome's `published_reason`, not its `reason`:
        a gated HOLD carries no instruction row, so this note is what the public
        snapshot publishes as the decision's thesis.
        """
        decision_id = new_decision_id()
        persist_pending_decision(self.session, decision_id)
        finalize_decision(
            self.session, decision_id, gate_result=gate_result, session_note=reason
        )
        return decision_id

    def _risk_check(
        self, positions: list[Any], account: Any, *, now: datetime, unified: Any
    ) -> list[Any]:
        tz = self.config.cadence.timezone
        trades_today, turnover_today = repo.trades_and_turnover_today(
            self.session, now=now, tz=tz
        )
        activity = RiskActivity(
            seen_client_order_ids=repo.seen_client_order_ids(self.session, now=now, tz=tz),
            trades_today=trades_today,
            turnover_today=turnover_today,
            day_trades_this_week=repo.day_trades_this_week(self.session, now=now, tz=tz),
            positions_opened_today=repo.positions_opened_today(self.session, now=now, tz=tz),
        )
        context = build_risk_context(
            self.config,
            account=account,
            positions=positions,
            last_prices=self.last_prices,
            symbol_status=self.symbol_status,
            activity=activity,
            now=now,
            kill_switch=self.kill_switch,
            risk=self._risk,
        )
        return check_batch(
            [item.trading_instruction for item in unified.instructions],
            context,
            risk=self._risk,
        )
