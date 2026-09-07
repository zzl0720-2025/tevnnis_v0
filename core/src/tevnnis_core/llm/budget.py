"""Budget guard — app-side daily token/call cap enforcement (§10).

Belt-and-suspenders companion to the provider-side prepaid hard ceiling: this
is the fast, in-app trip wire. It sums today's `api_usage` rows (kind="llm")
and refuses further LLM calls once either daily cap is reached, so the Router
can log a HOLD instead of spending a token past budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tevnnis_core.config import BudgetsConfig
from tevnnis_core.db.models import ApiUsage
from tevnnis_core.timeutil import day_bounds_utc


@dataclass(frozen=True)
class BudgetStatus:
    ok: bool
    tokens_used_today: int
    calls_used_today: int
    reason: str = ""


def check_budget(
    session: Session,
    budgets: BudgetsConfig,
    *,
    now: datetime,
    tz: str,
) -> BudgetStatus:
    """Today's usage against the configured caps. Does not write anything."""
    start, end = day_bounds_utc(now, tz)
    tokens_used, calls_used = session.execute(
        select(
            func.coalesce(func.sum(ApiUsage.tokens_in + ApiUsage.tokens_out), 0),
            func.coalesce(func.sum(ApiUsage.call_count), 0),
        ).where(
            ApiUsage.kind == "llm",
            ApiUsage.ts >= start,
            ApiUsage.ts <= end,
        )
    ).one()
    tokens_used, calls_used = int(tokens_used), int(calls_used)

    if calls_used >= budgets.llm_daily_call_cap:
        return BudgetStatus(False, tokens_used, calls_used, "llm_daily_call_cap reached")
    if tokens_used >= budgets.llm_daily_token_budget:
        return BudgetStatus(False, tokens_used, calls_used, "llm_daily_token_budget reached")
    return BudgetStatus(True, tokens_used, calls_used)


def usage_cost(
    tokens_in: int,
    tokens_out: int,
    *,
    price_in_per_mtok: float | None,
    price_out_per_mtok: float | None,
) -> float:
    """USD for one call, or 0.0 when the route carries no pricing.

    Prices are per 1M tokens, matching how every provider publishes them.
    A route with no pricing yields 0.0 rather than a guess — §7's `cost` column
    either holds a real figure or holds nothing, and the public snapshot reports
    null in the latter case rather than a confident $0.00 (see
    tevnnis_core/snapshot.py).
    """
    if price_in_per_mtok is None and price_out_per_mtok is None:
        return 0.0
    return (
        tokens_in / 1_000_000 * (price_in_per_mtok or 0.0)
        + tokens_out / 1_000_000 * (price_out_per_mtok or 0.0)
    )


def record_usage(
    session: Session,
    *,
    provider: str,
    tokens_in: int,
    tokens_out: int,
    cost: float = 0.0,
    now: datetime | None = None,
) -> None:
    """Append one api_usage ledger row for a completed LLM call.

    `now`, when given, is stamped explicitly rather than left to the DB's
    server-side clock, so a caller that also passed `now` to check_budget
    (e.g. the Router, for one consistent clock reading per decision round)
    gets a row that reliably falls inside that same day-boundary query.
    """
    row = ApiUsage(
        kind="llm",
        provider=provider,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost,
        call_count=1,
    )
    if now is not None:
        row.ts = now
    session.add(row)
