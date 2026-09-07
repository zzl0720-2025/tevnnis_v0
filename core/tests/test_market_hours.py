"""Market-hours boundaries (§9.1 step 7, §11 session input)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tevnnis_core.market_hours import (
    MarketSession,
    is_trading_time,
    next_open,
    open_close_epochs,
    seconds_until_open,
    session_for,
)

TZ = "America/New_York"
ET = ZoneInfo(TZ)


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


def test_sessions_across_a_weekday():
    # Wednesday 2026-09-02.
    assert session_for(at(2026, 9, 2, 3, 59), TZ) is MarketSession.CLOSED
    assert session_for(at(2026, 9, 2, 4, 0), TZ) is MarketSession.PRE_MARKET
    assert session_for(at(2026, 9, 2, 9, 29), TZ) is MarketSession.PRE_MARKET
    assert session_for(at(2026, 9, 2, 9, 30), TZ) is MarketSession.OPEN
    assert session_for(at(2026, 9, 2, 15, 59), TZ) is MarketSession.OPEN
    assert session_for(at(2026, 9, 2, 16, 0), TZ) is MarketSession.POST_MARKET
    assert session_for(at(2026, 9, 2, 19, 59), TZ) is MarketSession.POST_MARKET
    assert session_for(at(2026, 9, 2, 20, 0), TZ) is MarketSession.CLOSED


def test_weekend_is_always_closed():
    assert session_for(at(2026, 9, 5, 11, 0), TZ) is MarketSession.CLOSED  # Saturday
    assert session_for(at(2026, 9, 6, 11, 0), TZ) is MarketSession.CLOSED  # Sunday


def test_is_trading_time_is_regular_hours_only():
    assert is_trading_time(at(2026, 9, 2, 11, 0), TZ)
    assert not is_trading_time(at(2026, 9, 2, 8, 0), TZ)
    assert not is_trading_time(at(2026, 9, 2, 17, 0), TZ)


def test_session_is_computed_in_the_configured_timezone_not_utc():
    # 14:00 UTC is 10:00 ET — open — even though UTC would call it mid-afternoon.
    utc_now = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)
    assert session_for(utc_now, TZ) is MarketSession.OPEN


def test_open_close_epochs_bracket_the_regular_session():
    now = at(2026, 9, 2, 11, 0)
    open_ts, close_ts = open_close_epochs(now, TZ)
    assert open_ts == int(at(2026, 9, 2, 9, 30).timestamp())
    assert close_ts == int(at(2026, 9, 2, 16, 0).timestamp())
    assert open_ts < int(now.timestamp()) < close_ts


def test_next_open_skips_the_weekend():
    saturday = at(2026, 9, 5, 11, 0)
    assert next_open(saturday, TZ) == at(2026, 9, 7, 9, 30)  # Monday


def test_next_open_is_today_when_the_market_has_not_opened_yet():
    assert next_open(at(2026, 9, 2, 7, 0), TZ) == at(2026, 9, 2, 9, 30)


def test_next_open_is_tomorrow_once_the_session_has_started():
    assert next_open(at(2026, 9, 2, 11, 0), TZ) == at(2026, 9, 3, 9, 30)


def test_seconds_until_open():
    assert seconds_until_open(at(2026, 9, 2, 9, 0), TZ) == 30 * 60
