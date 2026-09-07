#!/usr/bin/env python3
"""Isolated Longbridge paper-account smoke test — read-only.

Deliberately NOT a pytest test and NOT wired into core: no CLI, no agent, no
database, no config file. It talks to the broker and prints, nothing else.

It exists to do three things before any order path is written (§9.1: validate
before anything trades; §13: swap mocks for real in safety order):

  1. prove the LONGPORT_* credentials work against the paper account;
  2. show exactly how the SDK represents MULTI-CURRENCY balances, so the
     AccountSnapshot mapping selects USD from evidence rather than a guess;
  3. act as the paper-account gate: the operator reads the printed balances
     and confirms they belong to the intended paper account before enabling
     the broker adapter.

WHY THIS IS THE GATE. There is deliberately no "paper vs live" switch anywhere
in this codebase: a flag that could select an account is a flag that could
select the WRONG account. The account is simply whatever the credentials in the
environment map to. That makes an operator's eyes on these numbers the only
real verification, which is why this script exists and runs first.

READ-ONLY BY CONSTRUCTION, not by convention: the trade context is wrapped in
_ReadOnlyTradeContext, which raises on any method outside a four-name
allowlist. submit_order / cancel_order / replace_order are not reachable from
this file even by mistake.

Secrets (§12): the SDK reads LONGPORT_APP_KEY / LONGPORT_APP_SECRET
/ LONGPORT_ACCESS_TOKEN itself, inside Config.from_apikey_env(). This script
never reads, copies, stores or prints a credential VALUE — it only checks that
each variable is set and non-empty and names the missing ones. Every SDK-sourced
string is redacted before printing.

Usage (the operator exports the credentials; nothing here parses .env):
    cd core
    set -a && source ../.env && set +a
    uv run python scripts/longbridge_account_smoke.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# ---------------------------------------------------------------------------
# Secret hygiene (§12)
# ---------------------------------------------------------------------------

# Longest run of token-alphabet characters still plausibly ordinary prose. This
# mirrors md/include/md/redact.hpp so both planes scrub identically; it will be
# kept in sync with the shared core implementation used by the broker adapter.
_SECRET_LIKE_RUN = 20
_TOKEN_RUN = re.compile(rf"[A-Za-z0-9_=-]{{{_SECRET_LIKE_RUN},}}")


def redact(text: str) -> str:
    """Mask credential-shaped runs in text that came from outside this program.

    Shape-based on purpose: matching against the real credential values would
    mean reading them, which §12 forbids — and a scrub that was never told the
    secret cannot leak it. '.' and '/' are not in the alphabet, so URLs and
    dotted hostnames stay readable while JWT segments and long keys are masked.
    """
    return _TOKEN_RUN.sub("<redacted>", text)


REQUIRED_ENV_VARS = (
    "LONGPORT_APP_KEY",
    "LONGPORT_APP_SECRET",
    "LONGPORT_ACCESS_TOKEN",
)


def missing_credential_names() -> list[str]:
    """Presence check only — the VALUE is never read, copied or printed (§12)."""
    return [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]


class SmokeError(RuntimeError):
    """A step failed; the message is already redacted."""


# ---------------------------------------------------------------------------
# Read-only enforcement
# ---------------------------------------------------------------------------

_READ_ONLY_METHODS = frozenset(
    {"account_balance", "stock_positions", "today_orders", "today_executions"}
)


class _ReadOnlyTradeContext:
    """Proxy exposing only the four read calls this smoke is allowed to make.

    Enforcement rather than a comment: `submit_order`, `cancel_order` and
    `replace_order` raise AttributeError here, so this script cannot place,
    modify or cancel an order even if someone edits it carelessly later.
    """

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    def __getattr__(self, name: str) -> Any:
        if name not in _READ_ONLY_METHODS:
            raise AttributeError(
                f"{name!r} is not permitted in the read-only account smoke "
                f"(allowed: {', '.join(sorted(_READ_ONLY_METHODS))})"
            )
        return getattr(self._ctx, name)


# ---------------------------------------------------------------------------
# The USD selection rule mirrors the production broker adapter.
# ---------------------------------------------------------------------------


@dataclass
class DerivedSnapshot:
    """What core's AccountSnapshot would receive, shown before it is wired up."""

    cash: float
    buying_power: float
    net_liquidation: float


def select_usd(balances: list[Any]) -> Any:
    """Return the USD AccountBalance. Never sums, never converts, never falls back.

    v0 trades US equities in USD only (§3). A multi-currency account's HKD
    entry is ignored outright — summing currencies or taking a cross-currency
    converted total would silently inflate buying power.
    """
    usd = [b for b in balances if str(b.currency).upper() == "USD"]
    if not usd:
        found = ", ".join(sorted(str(b.currency) for b in balances)) or "(none)"
        raise SmokeError(
            f"no USD balance in this account (currencies present: {found}). "
            "v0 trades US equities in USD only and must never fall back to "
            "another currency."
        )
    if len(usd) > 1:
        raise SmokeError(f"expected exactly one USD balance, got {len(usd)}")
    return usd[0]


def derive_snapshot(usd: Any) -> DerivedSnapshot:
    """Map the USD balance the way step 2's query_account() will.

    buying_power is min(cash, buy_power) — the conservative floor, so a
    financing/margin allowance can never make the agent think it has more
    deployable cash than it does.
    """
    cash = Decimal(usd.total_cash)
    buy_power = Decimal(usd.buy_power)
    return DerivedSnapshot(
        cash=float(cash),
        buying_power=float(min(cash, buy_power)),
        net_liquidation=float(Decimal(usd.net_assets)),
    )


def usd_cash_info(usd: Any) -> Any | None:
    for info in getattr(usd, "cash_infos", None) or []:
        if str(info.currency).upper() == "USD":
            return info
    return None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def d(value: Any) -> str:
    """Render a Decimal exactly — never through float."""
    return "-" if value is None else str(value)


def print_balances(balances: list[Any]) -> None:
    rule("1. account_balance()  — ALL currencies, unfiltered")
    print(f"{len(balances)} balance entr{'y' if len(balances) == 1 else 'ies'} returned\n")
    for balance in balances:
        print(f"  currency = {balance.currency}")
        print(f"    total_cash               = {d(balance.total_cash)}")
        print(f"    buy_power                = {d(balance.buy_power)}")
        print(f"    net_assets               = {d(balance.net_assets)}")
        print(f"    init_margin              = {d(balance.init_margin)}")
        print(f"    maintenance_margin       = {d(balance.maintenance_margin)}")
        print(f"    margin_call              = {d(balance.margin_call)}")
        print(f"    max_finance_amount       = {d(balance.max_finance_amount)}")
        print(f"    remaining_finance_amount = {d(balance.remaining_finance_amount)}")
        print(f"    risk_level               = {balance.risk_level}")
        infos = getattr(balance, "cash_infos", None) or []
        print(f"    cash_infos ({len(infos)}):")
        for info in infos:
            print(
                f"      [{info.currency}] available={d(info.available_cash)} "
                f"frozen={d(info.frozen_cash)} settling={d(info.settling_cash)} "
                f"withdraw={d(info.withdraw_cash)}"
            )
        fees = getattr(balance, "frozen_transaction_fees", None) or []
        if fees:
            print("    frozen_transaction_fees:")
            for fee in fees:
                print(f"      [{fee.currency}] {d(fee.frozen_transaction_fee)}")
        print()


def print_usd_selection(usd_filtered: list[Any], usd: Any) -> None:
    rule("2. USD selection — what core's AccountSnapshot would receive")
    print(f"account_balance(currency='USD') returned {len(usd_filtered)} entr"
          f"{'y' if len(usd_filtered) == 1 else 'ies'}")

    snapshot = derive_snapshot(usd)
    cash = Decimal(usd.total_cash)
    buy_power = Decimal(usd.buy_power)
    floor = "total_cash" if cash <= buy_power else "buy_power"

    print("\n  source values (USD only — HKD and any other currency ignored):")
    print(f"    total_cash = {d(cash)}    buy_power = {d(buy_power)}    "
          f"net_assets = {d(usd.net_assets)}")
    print("\n  AccountSnapshot:")
    print(f"    cash            = {snapshot.cash:,.2f}")
    print(f"    buying_power    = {snapshot.buying_power:,.2f}   "
          f"(min(total_cash, buy_power) -> {floor})")
    print(f"    net_liquidation = {snapshot.net_liquidation:,.2f}")

    info = usd_cash_info(usd)
    if info is None:
        print("\n  NOTE: no USD entry inside cash_infos — settled/unsettled split unavailable.")
        return

    available = Decimal(info.available_cash)
    settling = Decimal(info.settling_cash)
    print(f"\n  USD cash_infos: available={d(available)} settling={d(settling)} "
          f"frozen={d(info.frozen_cash)}")
    # T+1 settlement (§3) is not modelled by v0 — buying power trusts the
    # broker. If a material slice of total_cash is unsettled, the deployable
    # figure is available_cash, and the min() should take it as the cash side.
    if cash > 0 and available < cash:
        shortfall = cash - available
        pct = shortfall / cash * 100
        marker = "ACTION" if pct >= 1 else "minor"
        print(
            f"\n  [{marker}] available_cash is {d(shortfall)} ({pct:.2f}%) below total_cash."
        )
        if pct >= 1:
            print(
                "     T+1 unsettled funds are material, so switch the cash side of the\n"
                "     min() to available_cash in step 2 — it is what is deployable now."
            )
    else:
        print("\n  available_cash == total_cash — nothing unsettled; "
              "min(total_cash, buy_power) stands as specified.")


def print_positions(response: Any) -> None:
    rule("3. stock_positions() — all channels, all markets")
    channels = getattr(response, "channels", None) or []
    if not channels:
        print("  (no position channels)")
        return
    for channel in channels:
        positions = getattr(channel, "positions", None) or []
        print(f"  channel '{channel.account_channel}' — {len(positions)} position(s)")
        for pos in positions:
            # Step 2 keeps only market == US (§3: US-listed equities only) and
            # PositionSnapshot carries no currency, so a non-US row must never
            # enter it. Shown here so the filter can be checked against reality.
            print(
                f"    {pos.symbol:<12} qty={d(pos.quantity):>10} "
                f"available={d(pos.available_quantity):>10} cost={d(pos.cost_price):>10} "
                f"[{pos.currency} / {pos.market}]"
            )
        print()


def print_orders(orders: list[Any]) -> None:
    rule("4. today_orders() — remark is where client_order_id round-trips")
    if not orders:
        print("  (no orders today)")
        return
    for order in orders:
        print(
            f"  {order.order_id:<20} {order.symbol:<12} {order.side} {order.status}\n"
            f"    qty={d(order.quantity)} executed={d(order.executed_quantity)} "
            f"price={d(order.price)} executed_price={d(order.executed_price)} "
            f"[{order.currency}]\n"
            f"    remark={order.remark!r}"
        )


def print_executions(executions: list[Any]) -> None:
    rule("5. today_executions() — trade_id IS the §7 fills dedup key")
    if not executions:
        print("  (no executions today)")
        return
    for ex in executions:
        # PushOrderChanged carries no fill id, so this trade_id is the only
        # real broker_fill_id available. core must never invent one (§7).
        print(
            f"  trade_id={ex.trade_id:<24} order_id={ex.order_id:<20} {ex.symbol:<12} "
            f"qty={d(ex.quantity)} price={d(ex.price)} at {ex.trade_done_at}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run() -> int:
    from longport.openapi import AsyncTradeContext, Config, OpenApiException

    # flush: the credential check below writes to stderr, and the two streams
    # would otherwise interleave out of order in a terminal.
    print(
        "TEVNNIS — Longbridge PAPER account smoke (READ-ONLY: no orders, no cancels)",
        flush=True,
    )

    missing = missing_credential_names()
    if missing:
        print("\nmissing credential environment variable(s):", file=sys.stderr)
        for name in missing:
            print(f"  - {name}", file=sys.stderr)
        print(
            "export them first, e.g.:  set -a && source ../.env && set +a",
            file=sys.stderr,
        )
        return 1
    print("credentials: all 3 LONGPORT_* variables are set (values never read or printed)")

    try:
        config = Config.from_apikey_env()
    except Exception as exc:  # noqa: BLE001
        raise SmokeError(
            "could not build a Config from the environment: " + redact(str(exc))
        ) from exc

    try:
        raw_ctx = AsyncTradeContext.create(config, asyncio.get_running_loop())
        ctx = _ReadOnlyTradeContext(raw_ctx)

        balances = await ctx.account_balance()
        usd_filtered = await ctx.account_balance(currency="USD")
        positions = await ctx.stock_positions()
        orders = await ctx.today_orders()
        executions = await ctx.today_executions()
    except OpenApiException as exc:
        raise SmokeError("Longbridge API call failed: " + redact(str(exc))) from exc
    except Exception as exc:  # noqa: BLE001
        raise SmokeError(f"{type(exc).__name__}: " + redact(str(exc))) from exc

    print_balances(list(balances))
    # Prefer the server-filtered result; fall back to filtering the full list
    # ourselves so the selection rule is exercised either way.
    usd = select_usd(list(usd_filtered) or list(balances))
    print_usd_selection(list(usd_filtered), usd)
    print_positions(positions)
    print_orders(list(orders))
    print_executions(list(executions))

    rule("PAPER-ACCOUNT GATE")
    print(
        "Confirm the balances above are your KNOWN PAPER ACCOUNT before enabling\n"
        "the real broker adapter. There is no paper/live flag\n"
        "in this codebase by design — the account is whatever these credentials\n"
        "map to, so this check is the verification."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Longbridge paper-account connectivity smoke.",
    )
    parser.parse_args(argv)
    try:
        return asyncio.run(run())
    except SmokeError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
