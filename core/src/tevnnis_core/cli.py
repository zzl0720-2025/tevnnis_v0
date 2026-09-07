"""`tevnnis-core` — the attended command-line application (§9).

v0 is not a daemon. You start it during market hours, it tells you exactly what
it found and what it intends to do, you confirm, and it runs until you stop it
or the market closes. The default action is HOLD, so not running is always
safe (§9.3).

    Startup:  load config -> connect + validate every dependency -> reconcile
              against the broker (source of truth) -> health checks -> summary
              -> "Start TEVNNIS trading loop? [y/N]"
    Run:      one decision round per cadence tick, market hours respected
    Shutdown: stop pulling -> cancel in-flight orders -> flush -> summary

Everything printed is meant to be read by the operator: each step announces
itself, divergences are spelled out, and every round says what it did and why.

Offline demo (no Docker, no keys, no network):

    uv run tevnnis-core --config ../config/config.example.yaml \\
        --scenario ../config/scenario.example.json \\
        --database-url "sqlite:///:memory:" --yes --once

Three independent axes -- `--md-source`, `--broker`, `--llm` -- each default to
`mock`, so the bare invocation above IS the all-mock run. (There was a `--mock`
flag during early development. It was retired because it had stopped being true:
`--mock --broker longbridge` placed real orders, so the flag asserted nothing
while reading like a safety guarantee. What actually gates `--yes`/`--now` is
`guard_allows_auto_confirm`, which inspects the constructed adapters.)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tevnnis_core.agent import GATE_OK, AgentLoop, RoundReport
from tevnnis_core.config import StrategyConfig
from tevnnis_core.config_loader import load_config
from tevnnis_core.db import repository as repo
from tevnnis_core.db.models import Base, Order
from tevnnis_core.execution import SUBMITTED, ExecutionEngine
from tevnnis_core.llm.budget import check_budget
from tevnnis_core.llm.router import LLMRouter
from tevnnis_core.market_hours import (
    REGULAR_OPEN,
    SATURDAY,
    MarketSession,
    is_trading_time,
    next_open,
    session_for,
)
from tevnnis_core.risk_context import RiskEngineUnavailable, load_risk_module
from tevnnis_core.snapshot import (
    STATUS_RUNNING,
    STATUS_STOPPED,
    build_public_snapshot,
    write_public_snapshot,
)

DEFAULT_MD_ADDRESS = "127.0.0.1:50051"

# Run-loop verdicts for the market-hours gate.
TRADE = "trade"
WAIT = "wait"
STOP = "stop"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


@dataclass
class Dependencies:
    """Everything one run needs, already constructed.

    Nothing here declares "I am mocked" -- `guard_allows_auto_confirm` (used by
    both `--yes` and `--now`) checks the actual `trade_port`/`llm_provider`
    types instead of trusting a caller-supplied flag, and `describe_md_source`
    does the same for `md_client`. There is deliberately no separate
    `all_mocked` field to drift out of sync with those checks.
    """

    config: StrategyConfig
    session: Session
    md_client: Any
    trade_port: Any
    llm_provider: Any
    router: LLMRouter
    execution: ExecutionEngine
    agent: AgentLoop
    md_address: str = DEFAULT_MD_ADDRESS
    #: Where the §7/§14 public snapshot is written, or None when disabled.
    snapshot_dir: Path | None = None


@dataclass
class SessionStats:
    """Accumulated across the run for the end-of-session summary (§9.2)."""

    rounds: int = 0
    gate_results: Counter = field(default_factory=Counter)
    submitted: int = 0
    rejected_by_broker: int = 0
    rejected_by_risk: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    errors: int = 0

    def record(self, report: RoundReport) -> None:
        self.rounds += 1
        self.gate_results[report.gate_result] += 1
        self.tokens_in += report.tokens_in
        self.tokens_out += report.tokens_out
        self.rejected_by_risk += len(report.rejected)
        self.submitted += sum(1 for s in report.submissions if s.outcome == SUBMITTED)
        self.rejected_by_broker += sum(1 for s in report.submissions if s.outcome == "rejected")
        if report.error:
            self.errors += 1


def make_session(database_url: str) -> Session:
    """Open a session; create the schema directly for sqlite (alembic targets Postgres)."""
    engine = create_engine(database_url)
    if engine.dialect.name == "sqlite":
        Base.metadata.create_all(engine)
    return Session(engine)


def build_dependencies(
    config: StrategyConfig,
    *,
    session: Session,
    md_client: Any,
    trade_port: Any,
    llm_provider: Any,
    broker_name: str = "mock",
    llm_provider_name: str = "mock",
    llm_model_name: str | None = None,
    md_address: str = DEFAULT_MD_ADDRESS,
    risk_module: Any | None = None,
    snapshot_dir: Path | None = None,
) -> Dependencies:
    """Wire one run's dependencies.

    THE BROKER AND THE LLM ARE NAMED SEPARATELY, deliberately. An earlier
    implementation fed one `provider_name` to BOTH the Router and Execution,
    and it was derived from the `--broker` choice — so a `--broker longbridge`
    run wrote "longbridge" into `api_usage.provider` and `decisions.model_used`,
    mislabelling the LLM as the broker. The three axes (md, broker, llm) are
    independent, and so are their names.
    """
    router = LLMRouter(
        provider=llm_provider,
        budgets=config.budgets,
        tz=config.cadence.timezone,
        provider_name=llm_provider_name,
        model_name=llm_model_name,
        # §7's api_usage.cost either holds a real figure or holds nothing. When
        # llm_routing.strong carries no pricing these stay None, cost stays 0.0,
        # and the public snapshot reports ai_cost_today as null.
        price_in_per_mtok=config.llm_routing.strong.price_in_per_mtok,
        price_out_per_mtok=config.llm_routing.strong.price_out_per_mtok,
    )
    execution = ExecutionEngine(
        trade_port=trade_port, session=session, broker_name=broker_name
    )
    agent = AgentLoop(
        config=config,
        session=session,
        md_client=md_client,
        trade_port=trade_port,
        router=router,
        execution=execution,
        risk_module=risk_module,
    )
    return Dependencies(
        config=config,
        session=session,
        md_client=md_client,
        trade_port=trade_port,
        llm_provider=llm_provider,
        router=router,
        execution=execution,
        agent=agent,
        md_address=md_address,
        snapshot_dir=snapshot_dir,
    )


# ---------------------------------------------------------------------------
# §15 invariant: "no real broker, no real LLM spend" -- checked, not trusted.
# ---------------------------------------------------------------------------


def guard_allows_auto_confirm(deps: Dependencies) -> bool:
    """The one invariant that gates every non-interactive override (§15).

    Checks the actual adapter types rather than trusting a caller-supplied
    flag: both `--yes` (auto-confirm) and `--now` (clock override, see
    `now_override_allowed`) are refused unless `trade_port` is a `MockBroker`
    and `llm_provider` is a Mock LLM.

    `md_client` is deliberately NOT part of this check. A real
    `GrpcMarketDataClient` talking to a `tevnnis-md` subprocess -- even one
    replaying `MockMarketDataSource` -- is allowed under `--yes`, because md
    can neither place orders nor spend LLM budget: it can only widen what the
    agent sees, never what it is allowed to do.
    """
    from tevnnis_core.mocks.broker import MockBroker
    from tevnnis_core.mocks.llm import MockLLM

    return isinstance(deps.trade_port, MockBroker) and isinstance(deps.llm_provider, MockLLM)


def now_override_allowed(deps: Dependencies, now_override: datetime | None) -> bool:
    """`--now` is refused under the exact same invariant as `--yes`.

    Faking the clock is only ever safe when nothing it could feed a false
    "the market is open" reading into can spend anything real -- the Risk
    Engine's no-trade-window check reads `now` directly (§11), independent of
    `cadence.respect_market_hours`.
    """
    return now_override is None or guard_allows_auto_confirm(deps)


def describe_md_source(deps: Dependencies) -> str:
    """A short, honest label for the startup banner and logs (never guessed)."""
    if type(deps.md_client).__name__ == "GrpcMarketDataClient":
        return f"grpc @ {deps.md_address} (real tevnnis-md)"
    return "mock (scripted scenario)"


def describe_broker(deps: Dependencies) -> str:
    """Same idea for the broker: read the actual type, never a caller's claim."""
    if type(deps.trade_port).__name__ == "LongbridgeTradeBroker":
        return "Longbridge PAPER account (credentials from env)"
    if type(deps.trade_port).__name__ == "MockBroker":
        return "mock (in-memory)"
    return type(deps.trade_port).__name__


def describe_llm(deps: Dependencies) -> str:
    """And for the LLM — the axis that spends money rather than placing orders.

    A real LLM with a mock broker is still a LIVE run: the banner has to say so,
    or `LIVE -- broker=mock (in-memory)` would read as if nothing were real.
    """
    provider = deps.llm_provider
    if type(provider).__name__ == "MockLLM":
        return "mock (canned scenario)"
    describe = getattr(provider, "describe", None)
    return describe() if callable(describe) else type(provider).__name__


# ---------------------------------------------------------------------------
# §9.1 startup
# ---------------------------------------------------------------------------


class StartupError(RuntimeError):
    """A startup step failed — abort before trading (§9.1 step 2)."""


@dataclass
class ReconcileResult:
    broker_positions: list[Any]
    broker_open_orders: list[Any]
    db_live_orders: list[Any]
    divergences: list[str]


async def validate_dependencies(deps: Dependencies) -> list[str]:
    """§9.1 step 2 — every external dependency answers before anything trades."""
    checked: list[str] = []

    try:
        load_risk_module()
    except RiskEngineUnavailable as exc:
        raise StartupError(str(exc)) from exc
    checked.append("risk engine (C++ extension) importable")

    try:
        deps.session.execute(select(Order).limit(1))
    except Exception as exc:  # noqa: BLE001
        raise StartupError(f"database is not usable: {exc}") from exc
    checked.append("database reachable")

    validate = getattr(deps.md_client, "validate", None)
    if validate is not None:
        try:
            await validate()
        except Exception as exc:  # noqa: BLE001
            raise StartupError(f"market-data plane unreachable: {exc}") from exc
    checked.append(f"md plane answering at {deps.md_address} (source: {describe_md_source(deps)})")

    # A real adapter is constructed without touching the network (so the §15
    # guard tests can build one), so connecting is an explicit startup step --
    # the same `getattr` idiom the md client uses above.
    connect = getattr(deps.trade_port, "connect", None)
    if connect is not None:
        try:
            await connect()
        except Exception as exc:  # noqa: BLE001
            raise StartupError(f"broker not usable: {exc}") from exc

    try:
        account = await deps.trade_port.query_account()
        positions = await deps.trade_port.query_positions()
    except Exception as exc:  # noqa: BLE001
        raise StartupError(f"broker not usable: {exc}") from exc
    checked.append(
        f"broker connected: {describe_broker(deps)} -- USD cash "
        f"{account.cash:,.2f}, buying power {account.buying_power:,.2f}, "
        f"{len(positions)} position(s)"
    )
    for warning in getattr(deps.trade_port, "warnings", []):
        checked.append(f"NOTE {warning}")

    return checked


async def reconcile(deps: Dependencies, *, now: datetime) -> ReconcileResult:
    """§9.1 step 3 — the broker is the source of truth; the DB is corrected to match."""
    broker_positions = await deps.trade_port.query_positions()
    broker_open_orders = await deps.trade_port.query_open_orders()
    db_live = repo.live_orders(deps.session)

    divergences = repo.mirror_broker_positions(deps.session, broker_positions)

    broker_ids = {o.client_order_id for o in broker_open_orders}
    for order in broker_open_orders:
        if order.client_order_id not in {o.client_order_id for o in db_live}:
            divergences.append(
                f"order {order.client_order_id}: open at the broker, not live in the DB"
            )
        deps.execution.adopt(order, now=now)

    for order in db_live:
        if order.client_order_id not in broker_ids:
            divergences.append(
                f"order {order.client_order_id}: live in the DB ({order.status}), "
                "not open at the broker — marking cancelled"
            )
            repo.update_order(
                deps.session, order.client_order_id, status=repo.STATUS_CANCELLED
            )

    deps.session.commit()
    return ReconcileResult(broker_positions, broker_open_orders, db_live, divergences)


def render_startup_summary(
    deps: Dependencies, reconciled: ReconcileResult, *, now: datetime
) -> str:
    """§9.1 step 5 — the operator's picture of what is about to run."""
    config = deps.config
    tz = config.cadence.timezone
    lines: list[str] = []
    add = lines.append

    add("=" * 72)
    add("TEVNNIS — startup summary")
    add("=" * 72)
    add(f"  mode              : {config.account.mode.upper()}")
    add(
        f"  managed capital   : {config.account.managed_capital:,.2f} "
        f"{config.account.base_currency}"
    )
    add(f"  local time        : {now.astimezone(_zone(tz)).strftime('%Y-%m-%d %H:%M:%S')} ({tz})")
    add(f"  market session    : {session_for(now, tz).value}")

    budget = check_budget(deps.session, config.budgets, now=now, tz=tz)
    trades_today, turnover_today = repo.trades_and_turnover_today(
        deps.session, now=now, tz=tz
    )

    add("")
    add("  POSITIONS (from the broker — source of truth)")
    if not reconciled.broker_positions:
        add("    (none held)")
    for position in reconciled.broker_positions:
        last = deps.agent.last_prices.get(position.symbol)
        if last is not None and position.cost_basis:
            pnl = (last - position.cost_basis) / position.cost_basis * 100.0
            pnl_text = f"last {last:,.2f}  P&L {pnl:+.2f}%"
        else:
            pnl_text = "last n/a  P&L n/a (no quote yet this session)"
        add(
            f"    {position.symbol:<10} qty {position.quantity:>6}  "
            f"cost {position.cost_basis:>10,.2f}  {pnl_text}"
        )

    add("")
    add("  OPEN ORDERS (from the broker)")
    if not reconciled.broker_open_orders:
        add("    (none)")
    for order in reconciled.broker_open_orders:
        add(
            f"    {order.client_order_id}  {order.symbol or '?':<10} "
            f"{order.status}  broker id {order.broker_order_id or '-'}"
        )

    add("")
    add("  RECONCILE")
    if not reconciled.divergences:
        add("    DB agrees with the broker — no divergence.")
    for divergence in reconciled.divergences:
        add(f"    ! {divergence}")

    add("")
    add("  TODAY")
    add(
        f"    trades submitted : {trades_today} / {config.budgets.broker_max_trades_per_day}"
        f"   turnover {turnover_today:,.2f} / {config.budgets.broker_max_turnover_per_day:,.2f}"
    )
    add(
        f"    llm budget       : {budget.calls_used_today} / "
        f"{config.budgets.llm_daily_call_cap} calls, "
        f"{budget.tokens_used_today:,} / {config.budgets.llm_daily_token_budget:,} tokens"
    )
    if not budget.ok:
        add(f"    ! budget exhausted: {budget.reason} — the loop will HOLD without calling the LLM")

    add("")
    add("  CONFIG")
    add(f"    universe         : {sum(len(s) for s in config.universe.values())} symbols in "
        f"{len(config.universe)} sectors")
    add(f"    cadence          : every {config.cadence.decision_interval_seconds}s, "
        f"respect_market_hours={config.cadence.respect_market_hours}")
    add(f"    wake threshold   : reason_min_priority={config.cadence.reason_min_priority} "
        "(anything below this logs a HOLD without calling the strong model)")
    add(f"    risk limits      : position<={config.risk_limits.max_position_pct:.0%} "
        f"sector<={config.risk_limits.max_sector_pct:.0%} "
        f"cash>={config.risk_limits.min_cash_reserve_pct:.0%} "
        f"order<={config.risk_limits.max_order_notional:,.0f}")
    add(f"    shutdown         : cancel_open_orders_on_shutdown="
        f"{config.lifecycle.cancel_open_orders_on_shutdown}")
    add(f"    dependencies     : {_dependency_line(deps)}")
    add("=" * 72)
    return "\n".join(lines)


def _dependency_line(deps: Dependencies) -> str:
    """Distinguishes "everything mocked" from "safe for --yes" (§15).

    A real md source must never be reported as "ALL MOCKED" -- it isn't. What
    actually gates --yes/--now is guard_allows_auto_confirm (broker + LLM),
    independent of where events come from.
    """
    if not guard_allows_auto_confirm(deps):
        return f"LIVE -- broker={describe_broker(deps)}; llm={describe_llm(deps)}"
    md_source = describe_md_source(deps)
    if md_source.startswith("mock"):
        return "ALL MOCKED (no external calls)"
    return f"md={md_source}; broker/llm=MOCKED -- safe for --yes: no real broker, no real LLM spend"


def _zone(tz: str) -> Any:
    from zoneinfo import ZoneInfo

    return ZoneInfo(tz)


def confirm_start(
    deps: Dependencies,
    *,
    assume_yes: bool = False,
    prompt: Callable[[str], str] = input,
    out: Any = sys.stdout,
) -> bool:
    """§9.1 step 6 — the loop arms only on an explicit `y`.

    `--yes` (non-interactive auto-confirm) exists for test/CI use per §15,
    under one invariant: **no real broker, no real LLM spend** -- not "md must
    be in-process." See `guard_allows_auto_confirm`: it refuses outright
    unless `trade_port` is a MockBroker and `llm_provider` is a Mock LLM, so
    `--yes` can never arm a run that touches a real broker or spends real LLM
    budget. A real `GrpcMarketDataClient` talking to a `tevnnis-md` subprocess
    IS allowed here -- md can neither place orders nor spend budget, it can
    only widen what the agent sees.
    """
    if assume_yes:
        if not guard_allows_auto_confirm(deps):
            print(
                "REFUSED: --yes (non-interactive auto-confirm) is only allowed when the "
                "broker and the LLM provider are both mocks (no real orders, no real spend). "
                "Start interactively instead.",
                file=out,
            )
            return False
        print("Auto-confirmed (--yes; broker and LLM are mocked -- no real spend).", file=out)
        return True

    answer = prompt("Start TEVNNIS trading loop? [y/N] ")
    if answer.strip().lower() in ("y", "yes"):
        return True
    print("Not started.", file=out)
    return False


# ---------------------------------------------------------------------------
# §9.1 step 7 — the run loop
# ---------------------------------------------------------------------------


def market_gate(config: StrategyConfig, now: datetime) -> tuple[str, str]:
    """Whether to trade, wait for the open, or stop for the day (§9.1 step 7).

    Before the open we wait; after it we stop rather than idling overnight —
    §9 is an attended, per-session application, and restarting tomorrow is
    always safe (§9.3).
    """
    tz = config.cadence.timezone
    if not config.cadence.respect_market_hours:
        return TRADE, ""
    if is_trading_time(now, tz):
        return TRADE, ""

    session = session_for(now, tz)
    local = now.astimezone(_zone(tz))
    before_open = local.weekday() < SATURDAY and local.time() < REGULAR_OPEN
    if session == MarketSession.PRE_MARKET or before_open:
        opens = next_open(now, tz).strftime("%Y-%m-%d %H:%M")
        return WAIT, f"market is {session.value}; regular hours open at {opens} ({tz})"
    return STOP, f"market is {session.value} — regular session is over for today"


def render_round(report: RoundReport) -> str:
    """One self-explanatory line (plus detail) per decision round."""
    if report.error:
        return f"  [ERROR] round failed and was rolled back: {report.error}"

    head = (
        f"  events {report.events_pulled} (new {report.events_new}, "
        f"dropped {report.dropped_count}) -> {report.gate_result}"
    )
    lines = [head]
    if report.reason:
        lines.append(f"    reason: {report.reason}")
    if report.decision_id:
        lines.append(f"    decision {report.decision_id}")
    if report.tokens_in or report.tokens_out:
        lines.append(
            f"    model {report.model_used} tokens in {report.tokens_in} out {report.tokens_out}"
        )
    if report.expired:
        lines.append(f"    TIF expired, remainder cancelled: {', '.join(report.expired)}")
    if report.gate_result == GATE_OK:
        lines.append(
            f"    instructions {report.instructions}, allowed by risk {report.allowed}"
        )
    for rejected in report.rejected:
        lines.append(
            f"    REJECTED {rejected.action} {rejected.symbol}: "
            f"{rejected.rule_id} — {rejected.reason}"
        )
    for submission in report.submissions:
        lines.append(
            f"    ORDER {submission.client_order_id} {submission.outcome}"
            + (f" (broker {submission.broker_order_id})" if submission.broker_order_id else "")
            + (f" — {submission.message}" if submission.message else "")
        )
    if report.gate_result != GATE_OK or not report.submissions:
        lines.append("    no order placed this round")
    return "\n".join(lines)


@dataclass
class StopFlag:
    """Set by SIGINT; the loop finishes the round it is in and shuts down cleanly."""

    stopped: bool = False
    reason: str = ""

    def stop(self, reason: str) -> None:
        self.stopped = True
        self.reason = reason


async def run_loop(
    deps: Dependencies,
    *,
    stop: StopFlag,
    once: bool = False,
    max_rounds: int | None = None,
    now_fn: Callable[[], datetime] = utcnow,
    sleep: Callable[[float], Any] = asyncio.sleep,
    out: Any = sys.stdout,
) -> SessionStats:
    stats = SessionStats()
    interval = deps.config.cadence.decision_interval_seconds
    round_number = 0

    while not stop.stopped:
        if max_rounds is not None and round_number >= max_rounds:
            stop.stop("max rounds reached")
            break

        now = now_fn()
        verdict, message = market_gate(deps.config, now)
        if verdict == STOP:
            stop.stop(message)
            print(f"\n[market] {message}", file=out)
            break
        if verdict == WAIT:
            print(f"[market] {message} — waiting {interval}s", file=out)
            await sleep(interval)
            continue

        round_number += 1
        print(f"\n[round {round_number}] {now.isoformat(timespec='seconds')}", file=out)
        report = await deps.agent.run_round(now=now)
        stats.record(report)
        print(render_round(report), file=out)
        await sample_and_publish(deps, now=now, status=STATUS_RUNNING, out=out)

        if once:
            stop.stop("--once")
            break
        if max_rounds is not None and round_number >= max_rounds:
            # Stop now rather than sleeping out a cadence we will never use.
            stop.stop(f"--max-rounds {max_rounds} reached")
            break
        await sleep(interval)

    return stats


# ---------------------------------------------------------------------------
# §7/§14 public snapshot
# ---------------------------------------------------------------------------


async def sample_and_publish(
    deps: Dependencies,
    *,
    now: datetime,
    status: str,
    out: Any = sys.stdout,
) -> list[Path] | None:
    """Record this round's samples, then write the sanitized public snapshot.

    Deliberately here in the CLI rather than inside `AgentLoop.run_round`:

      * §8 orders the loop by cost — the cheap gate has to come *before* state
        assembly — and publishing has nothing to do with deciding. Sampling here
        means the equity curve gets a point on HOLD rounds too, which is exactly
        when the curve is still moving and no decision is being made.
      * `run_round` keeps its "one round is one transaction" invariant intact.
      * §9.2 already specifies "write a final snapshot" at shutdown, so the CLI
        was always going to own one of these calls.

    NEVER RAISES. A publication problem — an unwritable directory, a broker read
    that fails — must not end a trading session; the dashboard simply goes
    stale, which is a state it already renders. Returns the written paths, or
    None if nothing was written.
    """
    if deps.snapshot_dir is None:
        return None
    try:
        # One extra broker read per cadence tick. §3's trade limit is 30 calls
        # per 30s against a 300s cadence, and this is not LLM spend.
        account = await deps.trade_port.query_account()
        prices = {
            symbol: (price, deps.agent.last_change_pct.get(symbol))
            for symbol, price in deps.agent.last_prices.items()
        }
        repo.record_samples(
            deps.session,
            now=now,
            equity=account.net_liquidation,
            cash=account.cash,
            prices=prices,
        )
        deps.session.commit()

        snapshot = build_public_snapshot(
            deps.session, config=deps.config, now=now, status=status
        )
        return write_public_snapshot(snapshot, deps.snapshot_dir)
    except Exception as exc:  # noqa: BLE001 — publication must never end a session
        deps.session.rollback()
        print(
            f"[snapshot] could not publish: {type(exc).__name__}: {exc}",
            file=out,
        )
        return None


# ---------------------------------------------------------------------------
# §9.2 graceful shutdown
# ---------------------------------------------------------------------------


async def shutdown(
    deps: Dependencies, stats: SessionStats, *, now: datetime, reason: str, out: Any = sys.stdout
) -> list[str]:
    """Stop pulling, settle in-flight orders, flush, and explain the session."""
    print("\n[shutdown] stopping the decision loop...", file=out)
    cancelled: list[str] = []

    try:
        await deps.execution.poll_updates(now=now)
        if deps.config.lifecycle.cancel_open_orders_on_shutdown:
            cancelled = await deps.execution.cancel_all_inflight(now=now)
            print(
                f"[shutdown] cancel_open_orders_on_shutdown=true — cancelled "
                f"{len(cancelled)} in-flight order(s)",
                file=out,
            )
        elif deps.execution.inflight_ids:
            print(
                "[shutdown] cancel_open_orders_on_shutdown=false — leaving "
                f"{len(deps.execution.inflight_ids)} order(s) working at the broker",
                file=out,
            )

        positions = await deps.trade_port.query_positions()
        repo.mirror_broker_positions(deps.session, positions)
        deps.session.commit()
    except Exception as exc:  # noqa: BLE001 — always print the summary
        deps.session.rollback()
        print(f"[shutdown] error while settling: {type(exc).__name__}: {exc}", file=out)
        positions = []

    # §9.2: "write a final snapshot". Marked stopped, so the dashboard says the
    # system is down rather than merely stale.
    await sample_and_publish(deps, now=now, status=STATUS_STOPPED, out=out)

    print(render_session_summary(deps, stats, positions, now=now, reason=reason), file=out)

    for component in (deps.md_client, deps.trade_port):
        close = getattr(component, "close", None)
        if close is not None:
            try:
                await close()
            except Exception as exc:  # noqa: BLE001 — teardown must not raise
                print(
                    f"[shutdown] error closing {type(component).__name__}: "
                    f"{type(exc).__name__}: {exc}",
                    file=out,
                )
    deps.session.close()
    return cancelled


def render_session_summary(
    deps: Dependencies,
    stats: SessionStats,
    positions: list[Any],
    *,
    now: datetime,
    reason: str,
) -> str:
    tz = deps.config.cadence.timezone
    fills, fees = repo.fills_summary_today(deps.session, now=now, tz=tz)
    trades_today, turnover_today = repo.trades_and_turnover_today(
        deps.session, now=now, tz=tz
    )
    lines = ["", "=" * 72, "TEVNNIS — end of session", "=" * 72]
    lines.append(f"  stopped because  : {reason}")
    lines.append(f"  rounds run       : {stats.rounds}")
    if stats.gate_results:
        breakdown = ", ".join(
            f"{result} x{count}" for result, count in sorted(stats.gate_results.items())
        )
        lines.append(f"  decisions        : {breakdown}")
    lines.append(
        f"  orders           : {stats.submitted} submitted, "
        f"{stats.rejected_by_risk} rejected by risk, "
        f"{stats.rejected_by_broker} rejected by the broker"
    )
    lines.append(f"  fills today      : {fills} (fees {fees:,.2f})")
    lines.append(
        f"  trades today     : {trades_today} / {deps.config.budgets.broker_max_trades_per_day}"
        f"   turnover {turnover_today:,.2f}"
    )
    lines.append(f"  llm tokens       : in {stats.tokens_in:,} out {stats.tokens_out:,}")
    if stats.errors:
        lines.append(f"  ! rounds rolled back: {stats.errors}")
    lines.append("")
    lines.append("  FINAL POSITIONS (broker)")
    if not positions:
        lines.append("    (none held)")
    for position in positions:
        lines.append(
            f"    {position.symbol:<10} qty {position.quantity:>6} "
            f"cost {position.cost_basis:>10,.2f}"
        )
    lines.append("=" * 72)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tevnnis-core",
        description="TEVNNIS — attended, low-frequency trading agent (v0).",
    )
    parser.add_argument("--config", required=True, help="path to the §6 strategy YAML")
    parser.add_argument(
        "--md-address",
        default=os.environ.get("TEVNNIS_MD_ADDRESS", DEFAULT_MD_ADDRESS),
        help="host:port of tevnnis-md (env: TEVNNIS_MD_ADDRESS)",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="SQLAlchemy database URL (env: DATABASE_URL)",
    )
    parser.add_argument(
        "--md-source",
        choices=["mock", "grpc"],
        default="mock",
        help=(
            "mock (default): scripted market_data from --scenario. "
            "grpc: connect to a real tevnnis-md process at --md-address instead; "
            "--scenario then supplies only broker/llm (§2.2 wiring)."
        ),
    )
    parser.add_argument(
        "--broker",
        choices=["mock", "longbridge"],
        default="mock",
        help=(
            "mock (default): the in-memory MockBroker from --scenario. "
            "longbridge: place REAL orders on the Longbridge paper account "
            "(credentials from env; requires account.mode: paper in --config). "
            "A real broker makes --yes and --now refused -- live trading is "
            "interactive-confirm only (§15)."
        ),
    )
    parser.add_argument(
        "--llm",
        choices=["mock", "openai"],
        default="mock",
        help=(
            "mock (default): the canned MockLLM from --scenario, zero cost. "
            "openai: the real model named by llm_routing.strong in --config "
            "(OPENAI_API_KEY from env). A real LLM makes --yes and --now "
            "refused -- REAL SPEND is interactive-confirm only (§15)."
        ),
    )
    parser.add_argument(
        "--scenario",
        help=(
            "JSON scenario driving whichever mocks are still in play. With "
            "--llm openai its `llm` block is ignored; with --broker longbridge "
            "its `broker` block is ignored."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "skip the confirmation prompt; refused unless the broker and the LLM "
            "provider are both mocks (§15 — see guard_allows_auto_confirm)"
        ),
    )
    parser.add_argument(
        "--now",
        help=(
            "override the wall clock with this ISO 8601 datetime (must include a UTC "
            "offset), e.g. 2026-09-02T11:00:00-04:00. Test-only: refused under the same "
            "invariant as --yes, because the Risk Engine's no-trade-window check reads "
            "the real clock regardless of cadence.respect_market_hours."
        ),
    )
    parser.add_argument(
        "--snapshot-dir",
        help=(
            "where to write the sanitized public snapshot "
            "(public_snapshot.json + .js). Defaults to public_snapshot.output_dir "
            "in --config. The dashboard at frontend/dashboard.html reads the .js "
            "from its own directory."
        ),
    )
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="do not write the public snapshot at all",
    )
    parser.add_argument("--once", action="store_true", help="run a single decision round")
    parser.add_argument("--max-rounds", type=int, help="stop after N rounds")
    return parser.parse_args(argv)


def _resolve_now_override(args: argparse.Namespace) -> datetime | None:
    """Parse `--now`, or return None. Raises StartupError on anything unusable."""
    if not args.now:
        return None
    try:
        parsed = datetime.fromisoformat(args.now)
    except ValueError as exc:
        raise StartupError(f"--now is not a valid ISO 8601 datetime: {args.now!r}") from exc
    if parsed.tzinfo is None:
        raise StartupError(
            "--now must include a UTC offset, e.g. 2026-09-02T11:00:00-04:00 "
            "(a naive datetime is ambiguous across the configured timezone)"
        )
    return parsed


def _build_from_args(args: argparse.Namespace) -> Dependencies:
    config = load_config(args.config)

    if not args.database_url:
        raise StartupError(
            "no database URL: pass --database-url or set DATABASE_URL "
            '(try --database-url "sqlite:///:memory:" for a mock run)'
        )
    session = make_session(args.database_url)

    from tevnnis_core.mocks.scenario import ScenarioMocks, build_mocks, load_scenario

    mocks: ScenarioMocks = (
        load_scenario(args.scenario) if args.scenario else build_mocks({})
    )

    md_client: Any
    if args.md_source == "grpc":
        from tevnnis_core.market_data import GrpcMarketDataClient

        md_client = GrpcMarketDataClient(args.md_address)
    else:
        md_client = mocks.md_client

    trade_port: Any = mocks.broker
    if args.broker == "longbridge":
        # Fail closed before any real order can be placed. This is NOT a
        # paper/live selector -- it routes nothing and can only refuse. The
        # account is whatever the LONGPORT_* credentials map to; the operator
        # verifies which account that is with
        # scripts/longbridge_account_smoke.py.
        if config.account.mode != "paper":
            raise StartupError(
                "REFUSED: --broker longbridge requires account.mode: paper in "
                f"--config, but it says {config.account.mode!r}. v0 trades the "
                "Longbridge simulated account only."
            )
        from tevnnis_core.brokers.longbridge import LongbridgeTradeBroker

        # Constructed only -- connect() happens in validate_dependencies, so a
        # failure aborts startup before the loop can arm (§9.1).
        trade_port = LongbridgeTradeBroker()

    llm_provider: Any = mocks.llm
    llm_model_name: str | None = None
    if args.llm == "openai":
        strong = config.llm_routing.strong
        # The flag names the PROVIDER; the model comes from config. Assert they
        # agree rather than letting one silently win: a mismatch means the
        # operator believes they are running a model they are not.
        if strong.provider != args.llm:
            raise StartupError(
                f"REFUSED: --llm {args.llm} disagrees with --config, which says "
                f"llm_routing.strong.provider={strong.provider!r}. Point them at the "
                "same provider -- a run must never spend on a model you did not name."
            )
        from tevnnis_core.llm.openai_provider import (
            DEFAULT_REASONING_EFFORT,
            OpenAIProvider,
        )

        # Constructed only -- no key is read and no request is made here, so a
        # misconfigured run fails at startup rather than mid-session (§9.1).
        try:
            llm_provider = OpenAIProvider(
                model=strong.model,
                reasoning_effort=strong.reasoning_effort or DEFAULT_REASONING_EFFORT,
            )
        except ValueError as exc:
            raise StartupError(f"REFUSED: {exc}") from None
        llm_model_name = strong.model

    # --snapshot-dir wins over config; --no-snapshot beats both, and so does
    # `public_snapshot.enabled: false`.
    snapshot_dir: Path | None = None
    if not args.no_snapshot and config.public_snapshot.enabled:
        snapshot_dir = Path(args.snapshot_dir or config.public_snapshot.output_dir)

    return build_dependencies(
        config,
        session=session,
        md_client=md_client,
        trade_port=trade_port,
        llm_provider=llm_provider,
        broker_name=args.broker,
        llm_provider_name=args.llm,
        llm_model_name=llm_model_name,
        md_address=args.md_address,
        snapshot_dir=snapshot_dir,
    )


async def run(args: argparse.Namespace, out: Any = sys.stdout) -> int:
    print("[startup] 1/6 loading and validating configuration...", file=out)
    deps = _build_from_args(args)
    print(f"[startup]     config OK — mode={deps.config.account.mode}", file=out)
    print(f"[startup]     market data source: {describe_md_source(deps)}", file=out)
    print(f"[startup]     broker: {describe_broker(deps)}", file=out)
    print(f"[startup]     llm: {describe_llm(deps)}", file=out)

    now_override = _resolve_now_override(args)
    if not now_override_allowed(deps, now_override):
        raise StartupError(
            "REFUSED: --now (clock override) is only allowed when the broker and the LLM "
            "provider are both mocks (no real orders, no real spend) — the same invariant "
            "as --yes. Start without --now instead."
        )
    now_fn: Callable[[], datetime] = (
        (lambda fixed=now_override: fixed) if now_override is not None else utcnow
    )
    if now_override is not None:
        print(f"[startup]     clock overridden: --now {now_override.isoformat()}", file=out)

    print("[startup] 2/6 connecting and validating dependencies...", file=out)
    for check in await validate_dependencies(deps):
        print(f"[startup]     OK  {check}", file=out)

    now = now_fn()
    print("[startup] 3/6 reconciling against the broker (source of truth)...", file=out)
    reconciled = await reconcile(deps, now=now)
    print(
        f"[startup]     {len(reconciled.broker_positions)} position(s), "
        f"{len(reconciled.broker_open_orders)} open order(s), "
        f"{len(reconciled.divergences)} divergence(s)",
        file=out,
    )

    print("[startup] 4/6 health checks...", file=out)
    tz = deps.config.cadence.timezone
    budget = check_budget(deps.session, deps.config.budgets, now=now, tz=tz)
    print(
        f"[startup]     budget {'OK' if budget.ok else 'EXHAUSTED'}; "
        f"market {session_for(now, tz).value}; clock {now.isoformat(timespec='seconds')} UTC",
        file=out,
    )

    print("[startup] 5/6 summary\n", file=out)
    print(render_startup_summary(deps, reconciled, now=now), file=out)

    print("[startup] 6/6 confirmation", file=out)
    if not confirm_start(deps, assume_yes=args.yes, out=out):
        deps.session.close()
        return 1

    stop = StopFlag()
    _install_signal_handler(stop, out)

    stats = await run_loop(
        deps,
        stop=stop,
        once=args.once,
        max_rounds=args.max_rounds,
        now_fn=now_fn,
        out=out,
    )
    await shutdown(
        deps, stats, now=now_fn(), reason=stop.reason or "stopped", out=out
    )
    return 0


def _install_signal_handler(stop: StopFlag, out: Any) -> None:
    def handler(*_: Any) -> None:
        print("\n[signal] Ctrl-C received — finishing this round, then shutting down.", file=out)
        stop.stop("interrupted by the operator (Ctrl-C)")

    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, handler)
    except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover - platform
        signal.signal(signal.SIGINT, handler)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except StartupError as exc:
        print(f"\nSTARTUP ABORTED: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\nInterrupted before the loop armed.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
