"""NYSE session helpers shared by the scanner and the dashboard.

``market_is_open`` answers whether the exchange is in its regular session
right now (holidays and early closes excluded). ``expected_session_date``
answers which trading day's bars should exist as of now, so screens can
refuse stale data instead of screening yesterday's prices as today's.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytz

ET = pytz.timezone("America/New_York")

_CAL = None


def _calendar():
    """XNYS calendar, or None when pandas-market-calendars is unavailable."""
    global _CAL
    if _CAL is None:
        try:
            import pandas_market_calendars as mcal
        except ImportError:
            return None
        _CAL = mcal.get_calendar("XNYS")
    return _CAL


def market_is_open(now: datetime | None = None) -> bool:
    """True when now falls inside the NYSE regular session.

    Uses the exchange calendar when available (holidays and early closes
    excluded); falls back to weekday 09:30-16:00 ET otherwise.
    """
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = ET.localize(now)
    if now.weekday() >= 5:
        return False
    cal = _calendar()
    if cal is None:
        open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
        close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return open_t <= now <= close_t
    sched = cal.schedule(start_date=now.date(), end_date=now.date())
    if sched.empty:
        return False
    row = sched.iloc[0]
    return row["market_open"] <= now <= row["market_close"]


def expected_session_date(now: datetime | None = None):
    """Latest trading day whose session has started as of ``now``.

    A ticker whose latest bar predates this day is stale: its prices belong
    to an older session and must not be screened as current. Returns None
    when the calendar cannot answer (fail-open: the staleness check is then
    skipped rather than blocking the whole scan).
    """
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = ET.localize(now)
    cal = _calendar()
    if cal is None:  # no holiday data: most recent weekday
        day = now.date()
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        return day
    start = (now - timedelta(days=10)).date()
    try:
        sched = cal.schedule(start_date=start, end_date=now.date())
    except Exception:
        return None
    if sched.empty:
        return None
    past = sched[sched["market_open"] <= now]
    if past.empty:
        return None
    return past.index[-1].date()
