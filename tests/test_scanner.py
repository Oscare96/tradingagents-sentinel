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
