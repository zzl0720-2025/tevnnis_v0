"""Pure Longbridge payload -> core type mappings.

Deliberately free of any `longport` import: every function here takes
duck-typed objects and reads SDK enums through `str()`, so the whole mapping is
unit-testable offline, with synthetic payloads, on a machine that has never
seen the SDK. The adapter in `longbridge.py` owns the network calls and does
no mapping of its own.

`str()` rather than the enum object itself is not a stylistic choice: the
SDK's enum values are **unhashable** (`TypeError: unhashable type:
'builtins.OrderStatus'`), so a dict keyed on them raises at import time.
`str(OrderStatus.Filled)` is `'OrderStatus.Filled'` and is stable.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from tevnnis_core.ports import AccountSnapshot, OrderSnapshot, PositionSnapshot
from tevnnis_core.redact import redact_secrets as redact_secrets  # re-export; see below

# ---------------------------------------------------------------------------
# Secret hygiene (§12)
# ---------------------------------------------------------------------------
#
# `redact_secrets` lives in `tevnnis_core.redact` because broker and LLM
# adapters share the same log-scrubbing boundary. Re-exported here for
# compatibility with existing imports.


class BrokerMappingError(RuntimeError):
    """A broker payload could not be mapped safely. Always fail closed."""


# ---------------------------------------------------------------------------
# Account — USD selection
# ---------------------------------------------------------------------------
#
# THE SHAPE, AS OBSERVED ON A REAL PAPER ACCOUNT:
#
#   account_balance() returns ONE AccountBalance, in the account's BASE
#   currency (HKD). The real per-currency amounts live ONLY inside its
#   `cash_infos` list:
#
#       AccountBalance(currency='HKD',
#                      total_cash=114282.92,   <-- WHOLE ACCOUNT, CONVERTED
#                      buy_power=114282.92,    <-- ditto
#                      net_assets=114282.92,   <-- ditto
#                      cash_infos=[CashInfo(currency='USD', available_cash=3000.00),
#                                  CashInfo(currency='HKD', available_cash=778991.60)])
#
# The 114,282.92 figure is the entire account -- including the ~778,991 HKD --
# expressed in USD. It is NOT a USD cash balance. All three of
# total_cash/buy_power/net_assets came back equal, which is what gives the
# aggregate away.
#
# TWO THINGS THAT LOOK RIGHT AND ARE NOT:
#   * `account_balance(currency="USD")` does NOT return USD holdings. It
#     returns that same converted whole-account aggregate. Never call it.
#   * `AccountBalance.total_cash / buy_power / net_assets` are base-currency
#     aggregates that fold in the HKD. Never read them.
#
# Why this matters beyond tidiness: `buying_power` gates order notional in the
# Risk Engine (risk/src/engine.cpp), so the 114k figure would have authorised
# orders ~38x larger than the account can actually fund in USD.
#
# THE RULE: USD comes from the USD entry of `cash_infos`, and nowhere else.

_USD = "USD"


def select_usd_cash_info(balances: Any) -> Any:
    """Return the single USD CashInfo across all balances. Fail closed.

    Never sums currencies, never converts, never falls back to another
    currency, and never reads a base-currency aggregate.
    """
    found = [
        info
        for balance in balances
        for info in (getattr(balance, "cash_infos", None) or [])
        if str(info.currency).upper() == _USD
    ]
    if not found:
        currencies = sorted(
            {
                str(info.currency).upper()
                for balance in balances
                for info in (getattr(balance, "cash_infos", None) or [])
            }
        )
        raise BrokerMappingError(
            "no USD entry in cash_infos (currencies present: "
            f"{', '.join(currencies) or 'none'}). v0 trades US equities in USD "
            "only and must never fall back to another currency or to the "
            "base-currency aggregate."
        )
    if len(found) > 1:
        raise BrokerMappingError(
            f"expected exactly one USD cash_infos entry, found {len(found)} — "
            "refusing to guess which is the tradable balance."
        )
    return found[0]


def _finite_float(value: Any, what: str) -> float:
    try:
        result = float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BrokerMappingError(f"{what} is not a number: {value!r}") from exc
    if not math.isfinite(result):
        raise BrokerMappingError(f"{what} is not finite: {value!r}")
    return result


def map_account(balances: Any) -> AccountSnapshot:
    """Map Longbridge balances into core's single-currency AccountSnapshot.

    All three fields come from the USD `cash_infos` entry's `available_cash`:

      cash            -- USD settled cash. `available_cash` (not a computed
                         total) is already the deployable, settled figure, so
                         T+1 unsettled funds (§3) are excluded by construction.
      buying_power    -- the same USD cash. Cash-only and conservative on
                         purpose: `cash_infos` carries no per-currency buying
                         power, and the account's HKD-backed financing
                         allowance (a large `max_finance_amount`) must never
                         leak into what the agent believes it can deploy in USD.
      net_liquidation -- USD cash as the v0 proxy. A true USD-only net would
                         need the market value of USD positions, and
                         TradeContext exposes only `cost_price`, not a live
                         price (that lives in the md plane, not the broker). It
                         is never the converted whole-account figure. No Risk
                         rule reads net_liquidation -- only the LLM's state
                         line and a finiteness check -- so the proxy is safe;
                         every sizing rule uses managed_capital and
                         buying_power instead.
    """
    usd = select_usd_cash_info(balances)
    cash = _finite_float(usd.available_cash, "USD available_cash")
    if cash < 0:
        raise BrokerMappingError(f"USD available_cash is negative: {cash}")
    return AccountSnapshot(buying_power=cash, cash=cash, net_liquidation=cash)


# ---------------------------------------------------------------------------
# Order status
# ---------------------------------------------------------------------------

STATUS_OPEN = "open"
STATUS_PARTIALLY_FILLED = "partially_filled"
STATUS_FILLED = "filled"
STATUS_CANCELLED = "cancelled"
STATUS_REJECTED = "rejected"

# All 18 OrderStatus members, verified against the installed longport 4.3.7.
_STATUS_BY_NAME: dict[str, str] = {
    # Working: acknowledged, or an amendment in flight.
    "OrderStatus.New": STATUS_OPEN,
    "OrderStatus.WaitToNew": STATUS_OPEN,
    "OrderStatus.NotReported": STATUS_OPEN,
    "OrderStatus.ReplacedNotReported": STATUS_OPEN,
    "OrderStatus.ProtectedNotReported": STATUS_OPEN,
    "OrderStatus.VarietiesNotReported": STATUS_OPEN,
    "OrderStatus.WaitToReplace": STATUS_OPEN,
    "OrderStatus.PendingReplace": STATUS_OPEN,
    "OrderStatus.Replaced": STATUS_OPEN,
    # A cancel has been REQUESTED but not confirmed — still working, not terminal.
    "OrderStatus.WaitToCancel": STATUS_OPEN,
    "OrderStatus.PendingCancel": STATUS_OPEN,
    "OrderStatus.PartialFilled": STATUS_PARTIALLY_FILLED,
    "OrderStatus.Filled": STATUS_FILLED,
    # Terminal with an unfilled remainder. core has no "expired" status, and
    # PartialWithdrawal means "partly filled, remainder withdrawn" — the fills
    # themselves are recorded separately from the order's lifecycle status.
    "OrderStatus.Canceled": STATUS_CANCELLED,
    "OrderStatus.Expired": STATUS_CANCELLED,
    "OrderStatus.PartialWithdrawal": STATUS_CANCELLED,
    "OrderStatus.Rejected": STATUS_REJECTED,
    # Conservative: never declare an order terminal on a status we cannot read.
    "OrderStatus.Unknown": STATUS_OPEN,
}

KNOWN_STATUS_NAMES = frozenset(_STATUS_BY_NAME)


def map_order_status(status: Any) -> str:
    """Map a Longbridge OrderStatus to core's vocabulary.

    An unrecognised status maps to "open": leaving an order live is recoverable
    (reconcile will correct it), whereas wrongly declaring it terminal drops it
    out of tracking while it may still execute.
    """
    return _STATUS_BY_NAME.get(str(status), STATUS_OPEN)


def is_unknown_status(status: Any) -> bool:
    """True when `status` is outside the mapped set — the adapter warns on these."""
    return str(status) not in KNOWN_STATUS_NAMES


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

_MARKET_US = "Market.US"


def map_positions(channels: Any) -> list[PositionSnapshot]:
    """Flatten position channels to US holdings only (§3: US-listed equities).

    PositionSnapshot carries no currency, so a HK row must never enter it — it
    would be silently treated as USD by Risk and by the LLM state line.

    A non-integral quantity raises rather than truncating: v0 never submits
    fractional quantities, so one can only arrive from outside, and silently
    rounding a real holding would corrupt every downstream exposure check.
    """
    out: list[PositionSnapshot] = []
    for channel in channels:
        for pos in getattr(channel, "positions", None) or []:
            if str(pos.market) != _MARKET_US:
                continue
            quantity = Decimal(str(pos.quantity))
            if quantity != quantity.to_integral_value():
                raise BrokerMappingError(
                    f"fractional position quantity for {pos.symbol}: {quantity}. "
                    "v0 handles whole shares only; refusing to truncate a real holding."
                )
            out.append(
                PositionSnapshot(
                    # §3: symbol format is identity in v0 (NVDA.US == broker symbol).
                    symbol=pos.symbol,
                    quantity=int(quantity),
                    cost_basis=_finite_float(pos.cost_price, f"cost_price for {pos.symbol}"),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Orders — remark carries our client_order_id
# ---------------------------------------------------------------------------

# Matches llm/instruction_unifier.make_client_order_id: "co_" + 24 hex chars.
CLIENT_ORDER_ID_RE = re.compile(r"^co_[0-9a-f]{24}$")


def client_order_id_from_remark(remark: Any) -> str | None:
    """Our client_order_id if this order is ours, else None.

    Longbridge does not enforce uniqueness on `remark`, so this is an
    identification mechanism, not an idempotency guarantee — see
    LongbridgeTradeBroker.submit_order for the three layers that provide that.
    """
    text = (remark or "").strip()
    return text if CLIENT_ORDER_ID_RE.match(text) else None


@dataclass
class OpenOrders:
    ours: list[OrderSnapshot]
    foreign: int  # open orders at the broker that are not TEVNNIS's


def map_open_orders(orders: Any) -> OpenOrders:
    """Split broker orders into ours (by remark) and everyone else's.

    An order whose remark is absent or not shaped like a client_order_id was
    not placed by TEVNNIS — most likely by hand in the Longbridge app. Those
    are counted and reported, never adopted: reconcile (§9.1 step 3) would
    otherwise take ownership of, and eventually cancel, someone else's order.
    """
    ours: list[OrderSnapshot] = []
    foreign = 0
    for order in orders:
        client_order_id = client_order_id_from_remark(getattr(order, "remark", None))
        if client_order_id is None:
            foreign += 1
            continue
        ours.append(
            OrderSnapshot(
                client_order_id=client_order_id,
                status=map_order_status(order.status),
                symbol=order.symbol,
                broker_order_id=order.order_id,
            )
        )
    return OpenOrders(ours=ours, foreign=foreign)


# ---------------------------------------------------------------------------
# Fees — Longbridge reports them cumulatively per order
# ---------------------------------------------------------------------------


class FeeLedger:
    """Turns `OrderDetail.charge_detail.total_amount` into per-fill fees.

    Longbridge reports charges cumulatively for an order, but core records a
    fee per fill, so each poll emits only what has not been attributed yet.
    """

    def __init__(self) -> None:
        self._emitted: dict[str, float] = {}

    def take(self, broker_order_id: str, cumulative: float) -> float:
        """Incremental fee for this order since the last call (never negative)."""
        already = self._emitted.get(broker_order_id, 0.0)
        delta = cumulative - already
        if delta <= 0.0:
            return 0.0
        self._emitted[broker_order_id] = cumulative
        return delta


def split_fee(total: float, quantities: list[int]) -> list[float]:
    """Split an incremental fee across executions in proportion to quantity.

    The residue lands on the last execution so the parts always re-sum to the
    total, whatever the rounding.
    """
    if not quantities:
        return []
    total_qty = sum(quantities)
    if total_qty <= 0 or total <= 0.0:
        return [0.0] * len(quantities)
    parts = [round(total * q / total_qty, 6) for q in quantities[:-1]]
    parts.append(round(total - sum(parts), 6))
    return parts
