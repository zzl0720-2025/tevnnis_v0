"""Calendar-day / calendar-week boundaries in the configured timezone.

The daily caps (§6 `budgets`, §11 trade count and turnover) and the PDT
week (§11 `max_day_trades_per_week`) are all defined in the operator's local
trading day, but every timestamp is stored in UTC. One definition of "today"
and "this week" lives here so the budget guard and the risk aggregates can
never drift apart.

`now` is always injected — nothing here reads the wall clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_UTC = ZoneInfo("UTC")


def local_now(now: datetime, tz: str) -> datetime:
    return now.astimezone(ZoneInfo(tz))


def day_bounds_utc(now: datetime, tz: str) -> tuple[datetime, datetime]:
    """The [start, end] of `now`'s calendar day in `tz`, expressed in UTC."""
    start_local = local_now(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local.replace(hour=23, minute=59, second=59, microsecond=999999)
    return start_local.astimezone(_UTC), end_local.astimezone(_UTC)


def week_bounds_utc(now: datetime, tz: str) -> tuple[datetime, datetime]:
    """The [Monday 00:00, Sunday 23:59:59] of `now`'s week in `tz`, in UTC.

    The PDT rule counts day trades in a rolling five *business* days; v0 uses a
    Monday-anchored calendar week, which is the conservative reading for a
    single attended session per day.
    """
    local = local_now(now, tz)
    start_local = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_local = (start_local + timedelta(days=6)).replace(
        hour=23, minute=59, second=59, microsecond=999999
    )
    return start_local.astimezone(_UTC), end_local.astimezone(_UTC)
