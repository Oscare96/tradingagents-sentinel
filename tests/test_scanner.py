"""Unit tests for the market scanner (no network, no LLM)."""

import pytest

from tradingagents.scanner.screens import Candidate
from tradingagents.scanner.universe import _normalize


@pytest.mark.unit
def test_normalize_yfinance_spelling():
    assert _normalize("BRK.B") == "BRK-B"
    assert _normalize("  aapl ") == "AAPL"


@pytest.mark.unit
def test_candidate_scoring_accumulates():
    c = Candidate(ticker="XYZ")
    c.add(2.0, "up 5% intraday")
    c.add(1.5, "relative volume 3x")
    assert c.score == pytest.approx(3.5)
    assert len(c.reasons) == 2


@pytest.mark.unit
def test_market_hours():
    from datetime import datetime

    import pytz

    from tradingagents.scanner.scan import market_is_open

    et = pytz.timezone("America/New_York")
    # A known weekday: 2026-09-30 is a Wednesday.
    assert market_is_open(et.localize(datetime(2026, 9, 30, 10, 0))) is True
    assert market_is_open(et.localize(datetime(2026, 9, 30, 8, 0))) is False
    assert market_is_open(et.localize(datetime(2026, 10, 3, 10, 0))) is False  # Saturday


@pytest.mark.unit
def test_deep_dive_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "openai")
    from tradingagents.scanner import deep_dive

    ok, note = deep_dive.deep_dive_available()
    assert ok is False
    assert "OPENAI_API_KEY" in note


def _synth_series(values, idx):
    import pandas as pd

    return pd.Series(list(values), index=pd.DatetimeIndex(idx))


def _stale_cache_scenario():
    """Daily cache written pre-market: daily bars end Friday, but the price
    session is Monday. prev_close must be Friday's close, not Thursday's."""
    import pandas as pd
    import pytz

    et = pytz.timezone("America/New_York")
    # 30 business days ending Friday 2026-09-25; Thu 90 -> Fri 100
    days = pd.bdate_range(end="2026-09-25", periods=30)
    closes = [90.0] * 28 + [90.0, 100.0]
    d_close = _synth_series(closes, days)
    d_vol = _synth_series([1_000_000.0] * 30, days)
    d_open = _synth_series([c - 0.5 for c in closes], days)
    # Monday 2026-09-28 session: first bar opens 102, last bar closes 96
    bars = [et.localize(__import__("datetime").datetime(2026, 9, 28, 9, 30)) + __import__("datetime").timedelta(minutes=15 * i) for i in range(6)]
    i_close = _synth_series([103.0, 101.0, 99.0, 98.0, 97.0, 96.0], bars)
    i_open = _synth_series([102.0, 103.0, 101.0, 99.0, 98.0, 97.0], bars)
    i_vol = _synth_series([50_000.0] * 6, bars)
    return d_close, d_vol, d_open, i_close, i_vol, i_open


@pytest.mark.unit
def test_prev_close_uses_session_before_latest_bar():
    from tradingagents.scanner.screens import _screen_one

    d_close, d_vol, d_open, i_close, i_vol, i_open = _stale_cache_scenario()
    c = _screen_one("XYZ", d_close, d_vol, d_open, i_close, i_vol, i_open)
    assert c is not None
    # vs Friday's 100, not Thursday's 90: (96-100)/100 = -4.0%
    assert c.facts["pct_change"] == pytest.approx(-4.0)


@pytest.mark.unit
def test_gap_uses_first_bar_open_not_close():
    from tradingagents.scanner.screens import _screen_one

    d_close, d_vol, d_open, i_close, i_vol, i_open = _stale_cache_scenario()
    c = _screen_one("XYZ", d_close, d_vol, d_open, i_close, i_vol, i_open)
    assert c is not None
    # (102 open - 100 prev close) / 100 = +2.0%; first-bar close (103) would give +3.0%
    assert c.facts["gap_pct"] == pytest.approx(2.0)


@pytest.mark.unit
def test_rsi_one_sided_series():
    import pandas as pd

    from tradingagents.scanner.screens import _rsi

    assert _rsi(pd.Series([float(i) for i in range(1, 60)])) == 100.0
    assert _rsi(pd.Series([float(i) for i in range(60, 0, -1)])) == 0.0
    assert _rsi(pd.Series([50.0] * 60)) == 50.0


@pytest.mark.unit
def test_market_hours_holidays():
    from datetime import datetime

    import pytz

    from tradingagents.scanner.scan import market_is_open

    et = pytz.timezone("America/New_York")
    # Thanksgiving 2026 and Christmas 2026 are Thursdays/Fridays -> closed
    assert market_is_open(et.localize(datetime(2026, 11, 26, 10, 0))) is False
    assert market_is_open(et.localize(datetime(2026, 12, 25, 10, 0))) is False
    # Day after Thanksgiving: early close at 13:00 ET
    assert market_is_open(et.localize(datetime(2026, 11, 27, 10, 0))) is True
    assert market_is_open(et.localize(datetime(2026, 11, 27, 14, 0))) is False
