"""US equity market hours in the configured timezone (§9.1 step 4, §9.7, §11).

Two consumers:
  - the CLI run loop, for `cadence.respect_market_hours` (only decide while the
    market is open; stop at the close);
  - the Risk Engine's `RiskContext`, which needs the session plus today's open
    and close timestamps for the §11 no-trade windows.

Pure functions of an injected `now` — nothing here reads the wall clock, so
every boundary is testable.

**Holidays are out of scope for v0** (documented limitation): weekends and the
regular-hours clock are honoured, but US market holidays and half-days are not.
On a holiday the loop believes the market is open and simply finds no events;
Risk's other checks still apply and the broker would reject an order anyway. A
real exchange calendar is added to the market-data path.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

# Regular trading hours, US equities.
PRE_MARKET_OPEN = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
POST_MARKET_CLOSE = time(20, 0)

SATURDAY = 5


class MarketSession(str, Enum):
    """Where the clock sits in the trading day. Mirrors risk::MarketSession."""

    CLOSED = "CLOSED"
    PRE_MARKET = "PRE_MARKET"
    OPEN = "OPEN"
    POST_MARKET = "POST_MARKET"


def _local(now: datetime, tz: str) -> datetime:
    return now.astimezone(ZoneInfo(tz))


def is_weekend(now: datetime, tz: str) -> bool:
    return _local(now, tz).weekday() >= SATURDAY


def session_for(now: datetime, tz: str) -> MarketSession:
    """The session `now` falls in, in timezone `tz`."""
    local = _local(now, tz)
    if local.weekday() >= SATURDAY:
        return MarketSession.CLOSED
    clock = local.time()
    if clock < PRE_MARKET_OPEN:
        return MarketSession.CLOSED
    if clock < REGULAR_OPEN:
        return MarketSession.PRE_MARKET
    if clock < REGULAR_CLOSE:
        return MarketSession.OPEN
    if clock < POST_MARKET_CLOSE:
        return MarketSession.POST_MARKET
    return MarketSession.CLOSED


def is_trading_time(now: datetime, tz: str) -> bool:
    """True only during regular hours — the only window v0 trades in."""
    return session_for(now, tz) == MarketSession.OPEN


def open_close_epochs(now: datetime, tz: str) -> tuple[int, int]:
    """Epoch seconds of `now`'s calendar-day regular open and close in `tz`.

    Returned for any calendar day, weekend included: Risk gates a non-OPEN
    session on the session value itself, and these two timestamps only size the
    open/close volatility windows.
    """
    local = _local(now, tz)
    zone = ZoneInfo(tz)
    open_dt = datetime.combine(local.date(), REGULAR_OPEN, tzinfo=zone)
    close_dt = datetime.combine(local.date(), REGULAR_CLOSE, tzinfo=zone)
    return int(open_dt.timestamp()), int(close_dt.timestamp())


def next_open(now: datetime, tz: str) -> datetime:
    """The next regular-hours open at or after `now` (skipping weekends)."""
    local = _local(now, tz)
    zone = ZoneInfo(tz)
    candidate = datetime.combine(local.date(), REGULAR_OPEN, tzinfo=zone)
    while candidate <= local or candidate.weekday() >= SATURDAY:
        candidate = datetime.combine(
            (candidate + timedelta(days=1)).date(), REGULAR_OPEN, tzinfo=zone
        )
    return candidate


def seconds_until_open(now: datetime, tz: str) -> int:
    return max(0, int((next_open(now, tz) - _local(now, tz)).total_seconds()))
