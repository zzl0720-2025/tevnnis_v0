"""Pure Longbridge -> core mappings. No SDK, no network.

The account fixtures reproduce the shape OBSERVED on the real paper account by
the account smoke test, not an assumed payload shape:

    ONE AccountBalance in the account's BASE currency (HKD), whose
    total_cash/buy_power/net_assets are the WHOLE ACCOUNT converted to USD
    (114,282.92 -- which folds in ~778,991 HKD), with the real per-currency
    amounts living only inside cash_infos.

The original plan read those aggregates as USD. Since buying_power gates order
notional in the Risk Engine, that would have authorised orders ~38x larger than
the account can fund in USD. These tests exist to keep that fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from tevnnis_core.brokers.mapping import (
    BrokerMappingError,
    FeeLedger,
    client_order_id_from_remark,
    is_unknown_status,
    map_account,
    map_open_orders,
    map_order_status,
    map_positions,
    redact_secrets,
    split_fee,
)

# ---------------------------------------------------------------------------
# Fakes shaped exactly like the SDK types
# ---------------------------------------------------------------------------


@dataclass
class FakeCashInfo:
    currency: str
    available_cash: Decimal
    frozen_cash: Decimal = Decimal("0")
    settling_cash: Decimal = Decimal("0")
    withdraw_cash: Decimal = Decimal("0")


@dataclass
class FakeAccountBalance:
    currency: str
    total_cash: Decimal
    buy_power: Decimal
    net_assets: Decimal
    cash_infos: list[FakeCashInfo] = field(default_factory=list)
    max_finance_amount: Decimal = Decimal("0")


def real_account() -> list[FakeAccountBalance]:
    """The observed paper account: HKD base, USD+HKD inside cash_infos."""
    return [
        FakeAccountBalance(
            currency="HKD",
            total_cash=Decimal("114282.92"),
            buy_power=Decimal("114282.92"),
            net_assets=Decimal("114282.92"),
            max_finance_amount=Decimal("500000.00"),
            cash_infos=[
                FakeCashInfo(currency="USD", available_cash=Decimal("3000.00")),
                FakeCashInfo(currency="HKD", available_cash=Decimal("778991.60")),
            ],
        )
    ]


class FakeStatus:
    """Mimics the SDK's unhashable enum: only str() is stable."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __str__(self) -> str:
        return f"OrderStatus.{self._name}"

    def __hash__(self):  # noqa: D105 - the real SDK enum raises here
        raise TypeError("unhashable type: 'builtins.OrderStatus'")


@dataclass
class FakeMarket:
    name: str

    def __str__(self) -> str:
        return f"Market.{self.name}"


@dataclass
class FakePosition:
    symbol: str
    quantity: Decimal
    cost_price: Decimal
    currency: str
    market: FakeMarket
    available_quantity: Decimal = Decimal("0")


@dataclass
class FakeChannel:
    account_channel: str
    positions: list[FakePosition]


@dataclass
class FakeOrder:
    order_id: str
    symbol: str
    status: FakeStatus
    remark: str


# ---------------------------------------------------------------------------
# Account — the corrected USD selection
# ---------------------------------------------------------------------------


def test_usd_comes_from_cash_infos_not_the_converted_aggregate():
    account = map_account(real_account())

    assert account.cash == 3000.00
    assert account.buying_power == 3000.00
    assert account.net_liquidation == 3000.00

    # The three ways this could go wrong, pinned explicitly.
    assert account.cash != 114282.92, "must never read the converted whole-account aggregate"
    assert account.cash != 778991.60, "must never read HKD"
    assert account.cash != 781991.60, "must never sum currencies"


def test_buying_power_never_inherits_the_financing_allowance():
    # The account advertises a large max_finance_amount, backed by HKD. It must
    # not reach USD buying power: buying_power is USD cash, full stop.
    balances = real_account()
    balances[0].max_finance_amount = Decimal("9999999.00")
    assert map_account(balances).buying_power == 3000.00


def test_hkd_only_account_raises_rather_than_falling_back():
    balances = [
        FakeAccountBalance(
            currency="HKD",
            total_cash=Decimal("778991.60"),
            buy_power=Decimal("778991.60"),
            net_assets=Decimal("778991.60"),
            cash_infos=[FakeCashInfo(currency="HKD", available_cash=Decimal("778991.60"))],
        )
    ]
    with pytest.raises(BrokerMappingError, match="no USD entry in cash_infos"):
        map_account(balances)


def test_an_account_with_no_cash_infos_at_all_raises():
    balances = [
        FakeAccountBalance(
            currency="USD",
            total_cash=Decimal("114282.92"),
            buy_power=Decimal("114282.92"),
            net_assets=Decimal("114282.92"),
            cash_infos=[],
        )
    ]
    # Even though the OUTER currency says USD, there is no per-currency cash to
    # trust — the outer figure is an aggregate. Fail closed.
    with pytest.raises(BrokerMappingError, match="no USD entry in cash_infos"):
        map_account(balances)


def test_ambiguous_duplicate_usd_entries_raise():
    balances = real_account()
    balances[0].cash_infos.append(
        FakeCashInfo(currency="USD", available_cash=Decimal("50.00"))
    )
    with pytest.raises(BrokerMappingError, match="exactly one USD"):
        map_account(balances)


def test_available_cash_is_used_so_unsettled_funds_are_excluded():
    # available_cash already excludes T+1 settling funds (§3), so the settled,
    # deployable figure is what reaches Risk.
    balances = real_account()
    balances[0].cash_infos[0] = FakeCashInfo(
        currency="USD",
        available_cash=Decimal("1200.00"),
        settling_cash=Decimal("1800.00"),
    )
    assert map_account(balances).cash == 1200.00


def test_negative_or_nonnumeric_usd_cash_raises():
    balances = real_account()
    balances[0].cash_infos[0] = FakeCashInfo(currency="USD", available_cash=Decimal("-1"))
    with pytest.raises(BrokerMappingError, match="negative"):
        map_account(balances)

    balances[0].cash_infos[0] = FakeCashInfo(currency="USD", available_cash="not-a-number")
    with pytest.raises(BrokerMappingError, match="not a number"):
        map_account(balances)


def test_currency_matching_is_case_insensitive():
    balances = real_account()
    balances[0].cash_infos[0] = FakeCashInfo(currency="usd", available_cash=Decimal("3000"))
    assert map_account(balances).cash == 3000.0


# ---------------------------------------------------------------------------
# Order status — all 18 members
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("New", "open"),
        ("WaitToNew", "open"),
        ("NotReported", "open"),
        ("ReplacedNotReported", "open"),
        ("ProtectedNotReported", "open"),
        ("VarietiesNotReported", "open"),
        ("WaitToReplace", "open"),
        ("PendingReplace", "open"),
        ("Replaced", "open"),
        ("WaitToCancel", "open"),
        ("PendingCancel", "open"),
        ("PartialFilled", "partially_filled"),
        ("Filled", "filled"),
        ("Canceled", "cancelled"),
        ("Expired", "cancelled"),
        ("PartialWithdrawal", "cancelled"),
        ("Rejected", "rejected"),
        ("Unknown", "open"),
    ],
)
def test_every_order_status_maps(name, expected):
    assert map_order_status(FakeStatus(name)) == expected


def test_all_eighteen_sdk_statuses_are_covered():
    """Guards against a future SDK adding a status we silently treat as open."""
    from tevnnis_core.brokers.mapping import KNOWN_STATUS_NAMES

    assert len(KNOWN_STATUS_NAMES) == 18


def test_a_cancel_request_is_not_yet_terminal():
    # WaitToCancel/PendingCancel mean "cancel requested, not confirmed". Calling
    # them terminal would drop the order out of tracking while it can still fill.
    assert map_order_status(FakeStatus("WaitToCancel")) == "open"
    assert map_order_status(FakeStatus("PendingCancel")) == "open"


def test_an_unrecognised_status_is_flagged_and_treated_as_open():
    unknown = FakeStatus("SomeFutureStatus")
    assert is_unknown_status(unknown)
    assert map_order_status(unknown) == "open"
    assert not is_unknown_status(FakeStatus("Filled"))


def test_status_mapping_never_hashes_the_enum():
    # The real SDK enum raises TypeError on hash(); FakeStatus reproduces that,
    # so this test fails loudly if the mapping regresses to a dict keyed on it.
    with pytest.raises(TypeError):
        hash(FakeStatus("Filled"))
    assert map_order_status(FakeStatus("Filled")) == "filled"


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


def test_positions_are_us_only():
    channels = [
        FakeChannel(
            account_channel="lb_papertrading",
            positions=[
                FakePosition("NVDA.US", Decimal("10"), Decimal("105.50"), "USD", FakeMarket("US")),
                FakePosition("700.HK", Decimal("500"), Decimal("320.00"), "HKD", FakeMarket("HK")),
            ],
        )
    ]
    positions = map_positions(channels)
    assert [p.symbol for p in positions] == ["NVDA.US"]
    assert positions[0].quantity == 10
    assert positions[0].cost_basis == 105.50


def test_positions_flatten_across_channels():
    us = FakeMarket("US")
    channels = [
        FakeChannel("a", [FakePosition("NVDA.US", Decimal("1"), Decimal("1"), "USD", us)]),
        FakeChannel("b", [FakePosition("AMD.US", Decimal("2"), Decimal("2"), "USD", us)]),
    ]
    assert [p.symbol for p in map_positions(channels)] == ["NVDA.US", "AMD.US"]


def test_a_fractional_position_raises_rather_than_truncating():
    channels = [
        FakeChannel(
            "lb_papertrading",
            [FakePosition("NVDA.US", Decimal("1.5"), Decimal("105.50"), "USD", FakeMarket("US"))],
        )
    ]
    with pytest.raises(BrokerMappingError, match="fractional position"):
        map_positions(channels)


def test_no_positions_is_not_an_error():
    assert map_positions([]) == []
    assert map_positions([FakeChannel("empty", [])]) == []


# ---------------------------------------------------------------------------
# Orders — remark identification
# ---------------------------------------------------------------------------

OURS = "co_" + "a1b2c3d4e5f6" * 2  # co_ + 24 hex


def test_remark_identifies_our_orders():
    assert client_order_id_from_remark(OURS) == OURS
    assert client_order_id_from_remark(f"  {OURS}  ") == OURS
    assert client_order_id_from_remark("") is None
    assert client_order_id_from_remark(None) is None
    assert client_order_id_from_remark("my manual order") is None
    assert client_order_id_from_remark("co_short") is None
    assert client_order_id_from_remark("co_" + "Z" * 24) is None


def test_foreign_orders_are_counted_but_never_adopted():
    orders = [
        FakeOrder("LB-1", "NVDA.US", FakeStatus("New"), OURS),
        FakeOrder("LB-2", "700.HK", FakeStatus("New"), "placed by hand"),
        FakeOrder("LB-3", "AMD.US", FakeStatus("New"), ""),
    ]
    mapped = map_open_orders(orders)
    assert [o.client_order_id for o in mapped.ours] == [OURS]
    assert mapped.ours[0].broker_order_id == "LB-1"
    assert mapped.ours[0].status == "open"
    assert mapped.foreign == 2


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------


def test_fee_ledger_emits_only_the_increment():
    ledger = FeeLedger()
    assert ledger.take("LB-1", 0.50) == 0.50
    assert ledger.take("LB-1", 0.50) == 0.0  # re-poll: nothing new
    assert ledger.take("LB-1", 1.25) == pytest.approx(0.75)
    assert ledger.take("LB-1", 0.10) == 0.0  # never negative


def test_fee_ledger_is_per_order():
    ledger = FeeLedger()
    assert ledger.take("LB-1", 1.00) == 1.00
    assert ledger.take("LB-2", 2.00) == 2.00
    assert ledger.take("LB-1", 1.00) == 0.0


def test_split_fee_is_proportional_and_re_sums():
    parts = split_fee(1.00, [30, 70])
    assert parts == pytest.approx([0.30, 0.70])
    assert sum(parts) == pytest.approx(1.00)

    odd = split_fee(1.00, [1, 1, 1])
    assert sum(odd) == pytest.approx(1.00)

    assert split_fee(0.0, [5]) == [0.0]
    assert split_fee(1.0, []) == []


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def test_secrets_are_redacted_but_diagnostics_survive():
    jwt = "eyJhbGciOiJIUzI1NiJ9.dGhpc2lzYXNlY3JldHRva2VuMDAw"
    assert "eyJhbGciOiJIUzI1NiJ9" not in redact_secrets(f"bad token {jwt}")

    url = "request to https://openapi.longportapp.com/v1/trade failed"
    assert redact_secrets(url) == url
    assert redact_secrets("rate limit exceeded: 30 calls/30s") == (
        "rate limit exceeded: 30 calls/30s"
    )
