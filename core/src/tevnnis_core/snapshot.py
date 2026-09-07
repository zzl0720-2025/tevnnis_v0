"""The sanitization boundary: DB -> the sanitized public snapshot (§7, §14).

This module is the ONLY thing in TEVNNIS that produces data destined to leave
the process. §14 puts the public frontend on a different machine with no broker
key, no LLM key, no write path and no DB connection — its whole attack surface
is whatever this file writes. So the rules here are stricter than ordinary
code, and they are rules, not preferences:

**1. EXPLICIT ALLOW-LIST PROJECTION.** Every leaf of the output dict is assigned
by name, from a scalar pulled out of a query result. There is no `asdict()`, no
`__dict__`, no `vars()`, no `dict(row)` and no `**` splat of any model row or
dataclass anywhere in this module — and there must never be one. That is what
makes "adding a column to a model cannot leak it" a structural guarantee rather
than a habit. `test_snapshot_denylist.py` pins it by adding an attribute to a
seeded row and asserting it does not appear.

**2. THE ALLOW-LIST IS TESTED, NOT DOCUMENTED.** `test_snapshot_allowlist.py`
walks the built dict and asserts set equality of keys at every node. A new key
fails the test; so does a missing one. If you add a field here, you add it there
too, deliberately.

**3. NO ACCOUNT-SIZE INFORMATION, IN ANY FORM.** Performance is indexed to 100.
Position size is a percentage of the position sleeve. Never a dollar equity,
never cash, never an absolute P&L, never a share count. The ONE permitted dollar
figure in the whole file is `footer.ai_cost_today` — LLM operating spend, which
says nothing about the account.

**4. NO RISK INTERNALS.** `risk_audit.rule_tripped` is never read by this
module. Not masked, not mapped — never selected. The published `risk` field is
one of exactly "cleared" | "blocked" | "none", derived from the boolean `allow`
column alone. Thresholds and config values from §6's `risk_limits`/`budgets`
appear nowhere.

**5. CONFIGURATION VALUES ARE REMOVED AT THE SOURCE, not here.** `thesis` is
not always model prose: a gated HOLD has no instruction row, so its
`decisions.session_note` — written by core itself — becomes the published
thesis. That note used to read "... below reason_min_priority=HIGH", and it was
masked ONLY because that happens to be a 24-character run of `[A-Za-z0-9_=-]`
and so tripped `redact_secrets`' 20-character credential-shape heuristic. One
added space and the wake threshold would have gone out to a world-readable
file. The fix is `GateOutcome.public_reason` in agent.py, which never puts the
value in the string; `forbidden_config_terms()` below covers the same ground by
NAME as defence in depth. Neither relies on how long a rendering happens to be.

**Residual risk, stated honestly.** For genuine model prose `scrub_thesis` is a
mitigation and not a proof. It is bounded three ways: the model never sees
`risk_limits` or `budgets` at all (§6's config split, enforced in
llm/protocol_unifier.py), so it cannot restate a threshold it was never shown;
it *does* see account dollars and share quantities, which is exactly what the
currency / long-digit / share-count patterns target; and the length cap bounds
any single leak. What remains is a small figure in prose ("trimmed 50"). The
deny-list test covers structural leaks, which is where a guarantee is actually
available; this one is a defence in depth.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tevnnis_core.config import StrategyConfig
from tevnnis_core.db.models import (
    ApiUsage,
    Decision,
    Event,
    Fill,
    Instruction,
    Order,
    PortfolioSample,
    Position,
    PriceSample,
    RiskAudit,
)
from tevnnis_core.redact import redact_secrets
from tevnnis_core.timeutil import day_bounds_utc, local_now

#: Bumped whenever the shape below changes incompatibly. The dashboard reads it.
SCHEMA_VERSION = 1

#: Lifecycle states the dashboard renders. "starting" exists so a snapshot
#: written before the loop arms is not mistaken for a running system.
STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"
STATUS_STARTING = "starting"

#: Written next to frontend/dashboard.html. The .js form exists so the page can
#: be opened by double-click: a `<script src>` that 404s fails silently, whereas
#: `fetch()` is CORS-blocked under file://. See write_public_snapshot.
SNAPSHOT_JSON = "public_snapshot.json"
SNAPSHOT_JS = "public_snapshot.js"
SNAPSHOT_GLOBAL = "__TEVNNIS_SNAPSHOT__"

#: The equity curve's windows, in days. None = since inception. "1D" is not
#: here: it uses the calendar-day boundary instead, so the 1D curve's start
#: point and the `today_pct` delta describe the same period. A trailing-24h
#: window would quietly disagree with the "Today" figure beside it.
_SERIES_WINDOWS: dict[str, int | None] = {"1W": 7, "1M": 30, "ALL": None}

_INDEX_BASE = 100.0

#: Only a scheme we can reason about. http is dropped outright rather than
#: upgraded: a published link must not silently downgrade a reader's transport.
_ALLOWED_URL_SCHEME = "https"
_MAX_URL_CHARS = 512

# -- thesis scrubbing --------------------------------------------------------
# Ordered most-specific first; each is applied to the whole string.
_CURRENCY = re.compile(r"[$€£¥]\s?\d[\d,]*(?:\.\d+)?")
_SHARE_COUNT = re.compile(r"\b\d[\d,]*\s+(?:shares?|units?)\b", re.IGNORECASE)
_LONG_DIGITS = re.compile(r"\d[\d,]{3,}(?:\.\d+)?")
_WHITESPACE = re.compile(r"\s+")
_MASK = "[redacted]"

#: What may sit between a config field name and its value, so the name and the
#: value are always masked together. Masking "reason_min_priority" but leaving
#: "= HIGH" behind would publish exactly the value this exists to hide.
#:
#: The value alternatives are matched CASE-SENSITIVELY (the term itself is not,
#: via the inline (?i:...) below). That matters: under a blanket IGNORECASE the
#: SCREAMING_CASE alternative would also match the lowercase word "is" in
#: "reason_min_priority is HIGH", consuming the separator instead of the value
#: and leaving HIGH in the published text.
_TERM_VALUE = (
    r"(?:\s*(?:[:=]|\bis\b|\bof\b|\bat\b)?\s*"
    r"(?:\d+(?:\.\d+)?%?|[A-Z][A-Z_]+))?"
)

#: A §4.1 priority level standing next to threshold/gate wording, with NO config
#: key named. This is the rephrasing case: "below the HIGH threshold" leaks the
#: wake threshold while mentioning no field name at all, so no name-based rule
#: can catch it.
#:
#: Deliberately narrow. A bare "highest priority in batch is MEDIUM" is an
#: OBSERVATION about the events that actually arrived, not a configuration
#: value, and it is published — so a priority word is masked only where the
#: surrounding words make it a threshold.
_LEVELS = r"LOW|MEDIUM|HIGH|CRITICAL"
_GATE_WORDS = r"wake threshold|threshold|gate|minimum|min priority|cut-?off"
_THRESHOLD_LEVEL = re.compile(
    # "HIGH threshold"
    rf"\b(?:{_LEVELS})\b(?=\s+(?:{_GATE_WORDS})\b)"
    # "threshold of HIGH" / "gate is HIGH" / "minimum HIGH" — group 1 is kept.
    rf"|((?:{_GATE_WORDS})\s+(?:of\s+|is\s+|at\s+)?)(?:{_LEVELS})\b",
    re.IGNORECASE,
)


def _mask_threshold_level(match: re.Match[str]) -> str:
    """Keep the gate wording, replace only the level beside it."""
    return (match.group(1) or "") + _MASK


def forbidden_config_terms() -> frozenset[str]:
    """Every §6 field name that must not reach the public snapshot.

    Unlike a news host, this value space IS enumerable — it is exactly the
    pydantic field sets — so a denylist over it is sound and cannot go stale:
    adding a limit to RiskLimitsConfig adds it here automatically.

    Three models, for three reasons:

      * `RiskLimitsConfig` / `BudgetsConfig` — §6 makes these invisible even to
        the LLM, so a well-behaved model cannot name one. This catches the case
        where a name reaches the prose anyway (an operator pasting one into
        `style.notes`, say).
      * `CadenceConfig` — because core ITSELF writes one of these into a gated
        HOLD's `session_note`, which becomes a published thesis. The value is
        now stripped at the source (agent.py's GateOutcome.public_reason); this
        covers the name and its value BY NAME rather than by the coincidence
        that `reason_min_priority=HIGH` happens to be a 24-character run and so
        trips redact_secrets' 20-character token heuristic. Rephrase that string
        with a space in it and the coincidence evaporates.
    """
    from tevnnis_core.config import BudgetsConfig, CadenceConfig, RiskLimitsConfig

    return (
        frozenset(RiskLimitsConfig.model_fields)
        | frozenset(BudgetsConfig.model_fields)
        | frozenset(CadenceConfig.model_fields)
    )


# ---------------------------------------------------------------------------
# Sanitizers — pure, and the two most security-relevant functions in the repo
# ---------------------------------------------------------------------------


def sanitize_news_url(
    url: str | None,
    *,
    allowed_hosts: Sequence[str] = (),
    keep_params: Sequence[str] = (),
) -> str | None:
    """Return a publishable url, or None if it cannot be made safe.

    The threat: a news url that happens to carry an auth or session token would
    otherwise land verbatim in a world-readable file. It is closed by REBUILDING
    the url from a small set of components rather than by stripping known-bad
    ones — an unforeseen component cannot survive by not being on a blacklist.

    The rules, in order:
      * scheme must be exactly https (http is dropped, never upgraded);
      * no userinfo — `https://user:pass@host/` is dropped entirely;
      * no port other than the https default;
      * host must be non-empty; if `allowed_hosts` is non-empty the host must
        equal one of them or be a subdomain of one;
      * the result is scheme + host + path, plus only the query parameters
        named in `keep_params`. Every other parameter and any fragment is gone.

    On `allowed_hosts` defaulting to EMPTY, meaning "any https host": every rule
    above closes the token threat on its own. A host allow-list is a *different*
    kind of restriction, and it is only sound when the domain space is
    enumerable. A news feed's is not — `NewsItem.url` may point at any wire
    service — so a Longbridge-only default would silently null every external
    article link and break the clickthrough with no error anywhere. The knob
    stays so it can be tightened on evidence from the news smoke.
    """
    if not url or not isinstance(url, str) or len(url) > _MAX_URL_CHARS:
        return None

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None

    if parts.scheme.lower() != _ALLOWED_URL_SCHEME:
        return None
    # netloc, not hostname: this catches userinfo before urlsplit hides it.
    if "@" in parts.netloc:
        return None
    try:
        port = parts.port
    except ValueError:  # malformed port
        return None
    if port is not None and port != 443:
        return None

    host = (parts.hostname or "").lower()
    if not host:
        return None
    if allowed_hosts:
        allowed = [h.lower().lstrip(".") for h in allowed_hosts if h]
        if not any(host == h or host.endswith("." + h) for h in allowed):
            return None

    if keep_params:
        wanted = set(keep_params)
        try:
            kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                    if k in wanted]
        except ValueError:
            kept = []
        query = urlencode(kept)
    else:
        query = ""

    rebuilt = f"{_ALLOWED_URL_SCHEME}://{host}{parts.path}"
    if query:
        rebuilt = f"{rebuilt}?{query}"
    # Fragment is never carried, by construction — it is simply not rebuilt.
    return rebuilt if len(rebuilt) <= _MAX_URL_CHARS else None


def scrub_thesis(
    text: str | None,
    *,
    max_chars: int = 400,
    forbidden_terms: Iterable[str] | None = None,
) -> str:
    """Mask account-shaped figures out of prose and cap its length.

    See the module docstring for what this does and does not guarantee.

    `forbidden_terms` are masked together with whatever value is quoted beside
    them. It DEFAULTS to `forbidden_config_terms()` rather than to nothing: a
    caller that forgets to pass it should get the protection anyway, since
    forgetting is precisely how the `reason_min_priority` leak survived. Pass an
    explicit `()` to opt out.
    """
    if not text:
        return ""
    if forbidden_terms is None:
        forbidden_terms = forbidden_config_terms()
    # redact_secrets first: it is the shared shape-based credential scrub (§12)
    # and its long-token alphabet would otherwise be broken up by our masks.
    # Its marker is normalized to ours so one string never shows two spellings
    # of "redacted".
    out = redact_secrets(str(text)).replace("<redacted>", _MASK)
    out = _CURRENCY.sub(_MASK, out)
    out = _SHARE_COUNT.sub(_MASK, out)
    out = _LONG_DIGITS.sub(_MASK, out)
    for term in forbidden_terms:
        # The term PLUS whatever value is quoted beside it, so the threshold
        # goes with the name. Both a number and an enum level are eaten, and
        # the separator is optional and space-tolerant, so all of
        #   "max_position_pct 0.25"      "max_position_pct: 25%"
        #   "reason_min_priority=HIGH"   "reason_min_priority = HIGH"
        # collapse to a single mask. Masking the name but leaving "= HIGH"
        # behind would publish exactly the value this exists to hide.
        out = re.sub(rf"(?i:\b{re.escape(term)}\b){_TERM_VALUE}", _MASK, out)
    # A rephrasing that drops the field name entirely still leaks the value:
    # "below the HIGH threshold" names no config key at all. A priority level
    # adjacent to threshold-ish wording is masked on its own.
    out = _THRESHOLD_LEVEL.sub(_mask_threshold_level, out)
    out = _WHITESPACE.sub(" ", out).strip()

    if len(out) > max_chars:
        clipped = out[:max_chars]
        # Prefer a word boundary, but never throw away most of the sentence to
        # find one.
        space = clipped.rfind(" ")
        if space > max_chars // 2:
            clipped = clipped[:space]
        out = clipped.rstrip(" ,.;:") + "…"
    return out


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _iso_utc(moment: datetime) -> str:
    """UTC ISO-8601 with a Z suffix and second precision."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aware(moment: datetime | None, *, fallback: datetime) -> datetime:
    """A tz-aware datetime; sqlite hands back naive ones for TIMESTAMP columns."""
    if moment is None:
        return fallback
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _hhmm(moment: datetime, tz: str) -> str:
    return local_now(moment, tz).strftime("%H:%M")


def _round(value: float | None, places: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), places)


def _downsample(values: list[float], limit: int) -> list[float]:
    """Evenly thin a series to at most `limit` points, always keeping the ends."""
    if len(values) <= limit:
        return values
    step = (len(values) - 1) / (limit - 1)
    picked = [values[round(i * step)] for i in range(limit)]
    picked[-1] = values[-1]
    return picked


@dataclass(frozen=True)
class _RoundTrip:
    """One closed position, for win_rate. Never published; a local tally only."""

    won: bool


# ---------------------------------------------------------------------------
# Section builders — each returns exactly its allow-listed keys
# ---------------------------------------------------------------------------


def _build_performance(
    session: Session, *, now: datetime, tz: str, max_points: int
) -> dict[str, Any]:
    rows = list(
        session.execute(
            select(PortfolioSample.ts, PortfolioSample.equity).order_by(
                PortfolioSample.ts, PortfolioSample.id
            )
        ).all()
    )
    samples = [(_aware(ts, fallback=now), float(equity)) for ts, equity in rows]

    day_start, _ = day_bounds_utc(now, tz)
    windows: list[tuple[str, list[tuple[datetime, float]]]] = [
        ("1D", [s for s in samples if s[0] >= day_start])
    ]
    for label, days in _SERIES_WINDOWS.items():
        windows.append(
            (label, samples if days is None else
             [s for s in samples if s[0] >= now - timedelta(days=days)])
        )

    series: dict[str, list[float]] = {}
    for label, window in windows:
        if len(window) < 2:
            # An honest empty beats a one-point flat line pretending to be a
            # curve. The dashboard renders "insufficient history".
            series[label] = []
            continue
        base = window[0][1]
        indexed = [round(_INDEX_BASE * equity / base, 2) for _, equity in window] if base > 0 \
            else []
        series[label] = _downsample(indexed, max_points)

    # Cumulative is measured from inception, whatever "ALL" starts at.
    index_current = _INDEX_BASE
    if len(samples) >= 2 and samples[0][1] > 0:
        index_current = round(_INDEX_BASE * samples[-1][1] / samples[0][1], 2)

    today_pct: float | None = None
    today = [equity for ts, equity in samples if ts >= day_start]
    if len(today) >= 2 and today[0] > 0:
        today_pct = _round((today[-1] - today[0]) / today[0] * 100.0)

    return {
        "index_current": index_current,
        "index_start": _INDEX_BASE,
        "cumulative_pct": _round(index_current - _INDEX_BASE),
        "today_pct": today_pct,
        "series": series,
    }


def _latest_prices(session: Session) -> dict[str, tuple[float, float | None]]:
    """Most recent (last, change_pct) per symbol."""
    newest = (
        select(PriceSample.symbol, func.max(PriceSample.id).label("id"))
        .group_by(PriceSample.symbol)
        .subquery()
    )
    rows = session.execute(
        select(PriceSample.symbol, PriceSample.last, PriceSample.change_pct).join(
            newest, PriceSample.id == newest.c.id
        )
    ).all()
    return {symbol: (float(last), change) for symbol, last, change in rows}


def _build_positions(session: Session, *, trend_points: int) -> list[dict[str, Any]]:
    holdings = list(
        session.execute(
            select(Position.symbol, Position.quantity)
            .where(Position.quantity > 0)
            .order_by(Position.symbol)
        ).all()
    )
    prices = _latest_prices(session)

    # `quantity` and the dollar product below are LOCAL VARIABLES and are never
    # assigned into the output. Only the resulting percentage escapes, and it is
    # a share of the position sleeve (the weights sum to ~100) rather than of
    # the portfolio including cash — which would disclose the cash fraction.
    values: dict[str, float] = {}
    for symbol, quantity in holdings:
        last = prices.get(symbol, (None, None))[0]
        if last is not None:
            values[symbol] = float(quantity) * last
    total_value = sum(values.values())

    out: list[dict[str, Any]] = []
    for symbol, _quantity in holdings:
        last, change_pct = prices.get(symbol, (None, None))
        trend = [
            float(price)
            for price in reversed(
                session.execute(
                    select(PriceSample.last)
                    .where(PriceSample.symbol == symbol)
                    .order_by(PriceSample.ts.desc(), PriceSample.id.desc())
                    .limit(trend_points)
                )
                .scalars()
                .all()
            )
        ]
        out.append(
            {
                "symbol": symbol,
                "weight_pct": (
                    _round(values.get(symbol, 0.0) / total_value * 100.0, 1)
                    if total_value > 0
                    else None
                ),
                "last": _round(last),
                "today_pct": _round(change_pct),
                # A single point is not a trend; the dashboard shows an em-dash.
                "trend": [round(p, 4) for p in trend] if len(trend) >= 2 else [],
            }
        )
    return out


def _closed_round_trips(session: Session) -> list[_RoundTrip]:
    """Walk fills with an average-cost ledger; a position returning to 0 closes.

    Joined through orders -> instructions for the action, because `fills` itself
    records no direction. A fill with no local instruction (an order adopted
    from the broker at reconcile) is skipped rather than guessed at.
    """
    rows = session.execute(
        select(Fill.quantity, Fill.price, Instruction.action, Instruction.symbol)
        .join(Order, Fill.order_id == Order.id)
        .join(Instruction, Order.instruction_id == Instruction.id)
        .order_by(Fill.ts, Fill.id)
    ).all()

    held: dict[str, tuple[float, float]] = {}  # symbol -> (qty, avg cost)
    realized: dict[str, float] = {}
    closed: list[_RoundTrip] = []

    for quantity, price, action, symbol in rows:
        if not symbol or quantity is None or price is None:
            continue
        qty, avg = held.get(symbol, (0.0, 0.0))
        if action == "BUY":
            new_qty = qty + float(quantity)
            if new_qty > 0:
                avg = (avg * qty + float(price) * float(quantity)) / new_qty
            held[symbol] = (new_qty, avg)
        elif action == "SELL":
            sold = min(float(quantity), qty)
            realized[symbol] = realized.get(symbol, 0.0) + sold * (float(price) - avg)
            remaining = qty - sold
            held[symbol] = (remaining, avg)
            if remaining <= 0:
                closed.append(_RoundTrip(won=realized.pop(symbol, 0.0) > 0))
                held.pop(symbol, None)
    return closed


def _build_telemetry(session: Session, *, now: datetime, tz: str) -> dict[str, Any]:
    start, end = day_bounds_utc(now, tz)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    events_seen = int(
        session.execute(
            select(func.count(Event.id)).where(
                Event.ingest_ts >= start_ms, Event.ingest_ts <= end_ms
            )
        ).scalar_one()
    )

    # Decisions today, split by whether any instruction actually proposed a trade.
    decision_ids = list(
        session.execute(
            select(Decision.decision_id).where(Decision.ts >= start, Decision.ts <= end)
        )
        .scalars()
        .all()
    )
    acting_ids = set(
        session.execute(
            select(Instruction.decision_id).where(
                Instruction.decision_id.in_(decision_ids), Instruction.action != "HOLD"
            )
        )
        .scalars()
        .all()
    ) if decision_ids else set()

    # An event "acted on" is one an instruction cited (§5 cited_event_ids).
    cited: set[str] = set()
    if decision_ids:
        for ids in session.execute(
            select(Instruction.cited_event_ids).where(
                Instruction.decision_id.in_(decision_ids)
            )
        ).scalars():
            if isinstance(ids, list):
                cited.update(str(i) for i in ids)

    risk_rows = list(
        session.execute(
            select(RiskAudit.allow).where(RiskAudit.ts >= start, RiskAudit.ts <= end)
        )
        .scalars()
        .all()
    )
    positions_open = int(
        session.execute(
            select(func.count(Position.id)).where(Position.quantity > 0)
        ).scalar_one()
    )

    closed = _closed_round_trips(session)
    win_rate = (
        round(sum(1 for trip in closed if trip.won) / len(closed), 4) if closed else None
    )

    return {
        "events_seen": events_seen,
        "events_acted": len(cited),
        "decisions_total": len(decision_ids),
        "decisions_hold": len(decision_ids) - len(acting_ids),
        "decisions_act": len(acting_ids),
        "risk_checks": len(risk_rows),
        "risk_cleared": sum(1 for allow in risk_rows if allow),
        "risk_blocked": sum(1 for allow in risk_rows if not allow),
        "positions_open": positions_open,
        "win_rate": win_rate,
    }


def _build_news(
    session: Session,
    *,
    now: datetime,
    tz: str,
    limit: int,
    allowed_hosts: Sequence[str],
    keep_params: Sequence[str],
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Event.event_ts, Event.symbol, Event.payload)
        .where(Event.type == "NEWS")
        .order_by(Event.event_ts.desc(), Event.id.desc())
        .limit(limit)
    ).all()

    out: list[dict[str, Any]] = []
    for event_ts, symbol, payload in rows:
        # payload is JSON from MessageToDict, which omits empty proto3 strings —
        # hence .get() throughout, never subscripting.
        fields = payload if isinstance(payload, dict) else {}
        headline = str(fields.get("title") or "").strip()
        if not headline:
            continue
        moment = datetime.fromtimestamp(int(event_ts) / 1000, tz=timezone.utc)
        out.append(
            {
                "time": _hhmm(moment, tz),
                "symbol": symbol or "",
                "headline": headline,
                "url": sanitize_news_url(
                    fields.get("url"),
                    allowed_hosts=allowed_hosts,
                    keep_params=keep_params,
                ),
            }
        )
    return out


def _risk_label(verdicts: Iterable[bool]) -> str:
    """"blocked" | "cleared" | "none" — derived from `allow` alone.

    `risk_audit.rule_tripped` is deliberately not selected anywhere in this
    module: a rule name is a risk-configuration internal (§6 keeps limits
    invisible even to the LLM), so it must not become visible to the public.
    """
    verdicts = list(verdicts)
    if not verdicts:
        return "none"
    return "cleared" if all(verdicts) else "blocked"


def _build_reasoning(
    session: Session,
    *,
    now: datetime,
    tz: str,
    limit: int,
    thesis_max_chars: int,
    model: str | None,
) -> dict[str, Any]:
    forbidden = forbidden_config_terms()
    decisions = list(
        session.execute(
            select(Decision.decision_id, Decision.ts, Decision.model_used, Decision.session_note)
            .order_by(Decision.ts.desc(), Decision.decision_id.desc())
            .limit(limit)
        ).all()
    )
    if not decisions:
        return {"latest": None, "recent": []}

    ids = [row[0] for row in decisions]
    instructions = list(
        session.execute(
            select(
                Instruction.id,
                Instruction.decision_id,
                Instruction.action,
                Instruction.symbol,
                Instruction.thesis,
            )
            .where(Instruction.decision_id.in_(ids))
            .order_by(Instruction.id)
        ).all()
    )
    verdicts_by_instruction: dict[int, list[bool]] = {}
    if instructions:
        for instruction_id, allow in session.execute(
            select(RiskAudit.instruction_id, RiskAudit.allow).where(
                RiskAudit.instruction_id.in_([row[0] for row in instructions])
            )
        ).all():
            verdicts_by_instruction.setdefault(instruction_id, []).append(bool(allow))

    by_decision: dict[str, list[tuple[int, str, str | None, str | None]]] = {}
    for instruction_id, decision_id, action, symbol, thesis in instructions:
        by_decision.setdefault(decision_id, []).append((instruction_id, action, symbol, thesis))

    def summarize(decision_id: str, session_note: str | None) -> tuple[str, str | None, str, str]:
        """(action, symbol, risk, thesis) for one decision."""
        items = by_decision.get(decision_id, [])
        # The acting instruction is the interesting one; otherwise it is a HOLD.
        acting = next((i for i in items if i[1] != "HOLD"), None)
        chosen = acting or (items[0] if items else None)
        verdicts = [v for i, *_ in items for v in verdicts_by_instruction.get(i, [])]
        if chosen is None:
            # A gated HOLD has no instruction row at all; its reason is the note.
            return "HOLD", None, _risk_label(verdicts), scrub_thesis(
                session_note, max_chars=thesis_max_chars, forbidden_terms=forbidden
            )
        return (
            chosen[1],
            chosen[2],
            _risk_label(verdicts),
            scrub_thesis(
                chosen[3] or session_note,
                max_chars=thesis_max_chars,
                forbidden_terms=forbidden,
            ),
        )

    recent: list[dict[str, Any]] = []
    for decision_id, ts, _model_used, note in decisions:
        action, _symbol, risk, thesis = summarize(decision_id, note)
        recent.append(
            {
                "time": _hhmm(_aware(ts, fallback=now), tz),
                "action": action,
                "thesis": thesis,
                "risk": risk,
            }
        )

    head_id, head_ts, head_model, head_note = decisions[0]
    action, symbol, risk, thesis = summarize(head_id, head_note)
    latest = {
        "ts": _iso_utc(_aware(head_ts, fallback=now)),
        "action": action,
        "symbol": symbol,
        # The model NAME is a label, not a secret; fall back to the configured
        # route so an un-modelled gated HOLD still says what would have run.
        "model": head_model or model,
        "risk": risk,
        "thesis": thesis,
    }
    return {"latest": latest, "recent": recent}


def _build_footer(
    session: Session, config: StrategyConfig, *, now: datetime, tz: str
) -> dict[str, Any]:
    universe = [symbol for symbols in config.universe.values() for symbol in symbols]
    held = list(
        session.execute(
            select(Position.symbol).where(Position.quantity > 0).order_by(Position.symbol)
        )
        .scalars()
        .all()
    )

    # §6 splits config into LLM-visible `style` and code-enforced limits. Only
    # the style LABELS are published — never a numeric risk or budget value.
    persona = [config.style.risk_appetite, config.style.holding_bias.replace("_", " ")]

    start, end = day_bounds_utc(now, tz)
    cost, rows = session.execute(
        select(func.coalesce(func.sum(ApiUsage.cost), 0.0), func.count(ApiUsage.id)).where(
            ApiUsage.kind == "llm", ApiUsage.ts >= start, ApiUsage.ts <= end
        )
    ).one()

    return {
        "universe": universe,
        "held": held,
        "persona": persona,
        "model": config.llm_routing.strong.model,
        # The ONE dollar figure permitted in this file: LLM operating spend,
        # which reveals nothing about account size. None (not 0.0) when the
        # route carries no pricing, so the dashboard shows an em-dash rather
        # than a confident and wrong $0.00.
        "ai_cost_today": _round(float(cost), 4) if rows and float(cost) > 0 else None,
    }


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


def build_public_snapshot(
    session: Session,
    *,
    config: StrategyConfig,
    now: datetime,
    status: str = STATUS_RUNNING,
    demo: bool = False,
) -> dict[str, Any]:
    """Project the DB into the sanitized public snapshot. See the module docstring.

    Read-only: this function issues SELECTs and nothing else.
    """
    settings = config.public_snapshot
    tz = config.cadence.timezone

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso_utc(now),
        "status": status,
        "demo": demo,
        "performance": _build_performance(
            session, now=now, tz=tz, max_points=settings.series_max_points
        ),
        "positions": _build_positions(session, trend_points=settings.trend_points),
        "telemetry": _build_telemetry(session, now=now, tz=tz),
        "news": _build_news(
            session,
            now=now,
            tz=tz,
            limit=settings.news_max_items,
            allowed_hosts=settings.news_url_allow_hosts,
            keep_params=settings.news_url_keep_params,
        ),
        "reasoning": _build_reasoning(
            session,
            now=now,
            tz=tz,
            limit=settings.recent_decisions,
            thesis_max_chars=settings.thesis_max_chars,
            model=config.llm_routing.strong.model,
        ),
        "footer": _build_footer(session, config, now=now, tz=tz),
    }


# ---------------------------------------------------------------------------
# Atomic publication
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then os.replace.

    The rename is atomic within a filesystem, so the dashboard — which may read
    at any moment, mid-round — never sees a half-written file. The temp file is
    created in the destination directory precisely so the rename stays
    intra-filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    )
    tmp = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_public_snapshot(snapshot: Mapping[str, Any], out_dir: str | Path) -> list[Path]:
    """Write public_snapshot.json and public_snapshot.js atomically. Returns both paths.

    Two formats from one dict, deliberately:
      * the .json is the canonical artifact — what §14's one-way publish ships;
      * the .js assigns it to a global so `frontend/dashboard.html` can load it
        with a plain `<script src>`. That is what lets the page work by
        double-click: a missing `<script src>` fails silently, whereas `fetch()`
        of a local file is CORS-blocked under file://.
    """
    directory = Path(out_dir)
    payload = json.dumps(snapshot, indent=2, sort_keys=False, allow_nan=False)

    json_path = directory / SNAPSHOT_JSON
    js_path = directory / SNAPSHOT_JS
    _atomic_write(json_path, payload + "\n")
    _atomic_write(
        js_path,
        "// Generated by tevnnis-core (tevnnis_core/snapshot.py). Do not edit.\n"
        f"window.{SNAPSHOT_GLOBAL} = {payload};\n",
    )
    return [json_path, js_path]
