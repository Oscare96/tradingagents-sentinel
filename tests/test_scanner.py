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


@pytest.mark.unit
def test_gap_unavailable_when_open_missing():
    """A missing opening print must not fabricate a gap from another session."""
    import pandas as pd

    from tradingagents.scanner.screens import _screen_one

    d_close, d_vol, d_open, i_close, i_vol, i_open = _stale_cache_scenario()
    i_open = pd.Series([float("nan")] * len(i_open), index=i_open.index)
    c = _screen_one("XYZ", d_close, d_vol, d_open, i_close, i_vol, i_open)
    assert c is not None  # still scores via the 52-week-high proximity screen
    assert c.facts["gap_pct"] is None
    assert not any("gapped" in r for r in c.reasons)
    assert c.facts["price_asof"] == i_close.index[-1].isoformat()


@pytest.mark.unit
def test_expected_session_date():
    from datetime import datetime

    import pytz

    from tradingagents.scanner.market_calendar import expected_session_date

    et = pytz.timezone("America/New_York")
    # Monday 10:00 ET: today's session has started -> Monday
    assert expected_session_date(
        et.localize(datetime(2026, 9, 28, 10, 0))).isoformat() == "2026-09-28"
    # Monday pre-market: the latest started session is Friday
    assert expected_session_date(
        et.localize(datetime(2026, 9, 28, 8, 0))).isoformat() == "2026-09-25"
    # Saturday: Friday
    assert expected_session_date(
        et.localize(datetime(2026, 10, 3, 12, 0))).isoformat() == "2026-10-02"


def _multi_ticker_frames():
    """AAA: intraday ends Friday (stale); BBB: intraday ends Monday (fresh)."""
    import pandas as pd
    import pytz

    et = pytz.timezone("America/New_York")
    days = pd.bdate_range(end="2026-09-25", periods=30)
    fri_bars = [et.localize(__import__("datetime").datetime(2026, 9, 25, 9, 30))
                + __import__("datetime").timedelta(minutes=15 * i) for i in range(6)]
    mon_bars = [et.localize(__import__("datetime").datetime(2026, 9, 28, 9, 30))
                + __import__("datetime").timedelta(minutes=15 * i) for i in range(6)]

    def frame(idx, closes, opens):
        n = len(idx)
        return pd.DataFrame(
            {"Open": opens, "High": closes, "Low": closes, "Close": closes,
             "Volume": [50_000.0] * n}, index=idx)

    daily = pd.concat({
        "AAA": frame(days, [50.0] * 30, [49.5] * 30),
        "BBB": frame(days, [90.0] * 28 + [90.0, 100.0], [89.5] * 30),
    }, axis=1)
    intraday = pd.concat({
        # AAA never traded Monday: its latest bar is Friday's
        "AAA": frame(fri_bars, [50.0] * 6, [49.5] * 6),
        # BBB gapped up Monday: Friday close 100 -> Monday open 103
        "BBB": frame(mon_bars,
                     [103.0, 103.5, 104.0, 104.0, 104.0, 104.0],
                     [103.0, 103.0, 103.5, 104.0, 104.0, 104.0]),
    }, axis=1)
    # _px expects (field, ticker) column order
    daily.columns = daily.columns.swaplevel(0, 1)
    intraday.columns = intraday.columns.swaplevel(0, 1)
    return daily, intraday


@pytest.mark.unit
def test_stale_ticker_skipped_by_run_screens(monkeypatch):
    from datetime import datetime

    import pytz

    import tradingagents.scanner.screens as smod

    monkeypatch.setattr(smod, "_batch_download",
                        lambda tickers: _multi_ticker_frames())
    et = pytz.timezone("America/New_York")
    now = et.localize(datetime(2026, 9, 28, 10, 0))  # Monday, market open
    cands, stats = smod.run_screens(["AAA", "BBB"], now=now)
    assert stats["stale_skipped"] == 1
    assert [c.ticker for c in cands] == ["BBB"]


@pytest.mark.unit
def test_deep_dive_summary_preserves_decisions_and_reports_partial():
    from tradingagents.scanner.scan import _summarize_deep_dive
    from tradingagents.scanner.screens import Candidate

    top = [Candidate(ticker="AAA"), Candidate(ticker="BBB")]
    analyses = [
        {"ticker": "AAA", "signal": "BUY", "decision": "long thesis text", "ok": True},
        {"ticker": "BBB", "signal": "ERROR", "decision": "boom", "ok": False},
    ]
    watchlist, note = _summarize_deep_dive(top, analyses, True, "")
    assert watchlist[0]["decision"] == "long thesis text"
    assert watchlist[0]["deep_dive"] is True
    assert watchlist[1]["deep_dive"] is False
    assert note == "deep dive partial: 1/2 succeeded"

    _, note_ok = _summarize_deep_dive(top, analyses[:1], True, "")
    assert note_ok == "deep dive completed"

    _, note_off = _summarize_deep_dive(top, [], False, "no key configured")
    assert note_off == "no key configured"


def _quiet_service(tmp_path, monkeypatch):
    from tradingagents.dashboard import scanner_service as mod

    monkeypatch.setattr(mod, "STATE_FILE", tmp_path / "state.json")
    svc = mod.ScannerService()
    monkeypatch.setattr(svc, "_do_scan", lambda: None)
    return svc


@pytest.mark.unit
def test_superseded_loop_exits_without_rescan(tmp_path, monkeypatch):
    """A stop -> start restart must not leave the old loop scheduling scans."""
    import threading

    svc = _quiet_service(tmp_path, monkeypatch)
    svc._generation = 1
    svc.stop()  # bumps the generation, orphaning the generation-1 loop
    t = threading.Thread(target=svc._loop, args=(1,), daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()


@pytest.mark.unit
def test_start_while_running_updates_interval_without_new_thread(tmp_path, monkeypatch):
    svc = _quiet_service(tmp_path, monkeypatch)
    svc.start(15)
    first_thread = svc._thread
    assert first_thread.is_alive()
    svc.start(30)
    assert svc._thread is first_thread
    assert svc.interval_min == 30
    svc.stop()
