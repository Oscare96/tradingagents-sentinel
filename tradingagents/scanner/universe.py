"""Scan universe: which tickers the 15-minute screens cover.

Scanning "the entire market" every 15 minutes is not feasible on free data:
thousands of tickers would blow through Yahoo Finance rate limits and most of
them are too illiquid to trade anyway. StocksToTrade solves the same problem
with curated universes and liquidity filters, so we do the same:

- Default universe: S&P 500 constituents (liquid, optionable, real volume).
- ``SCANNER_UNIVERSE`` env var: ``sp500`` (default) or a path to a text file
  with one ticker per line for a custom watchlist.
- Every screen additionally enforces a minimum price and minimum dollar-volume
  floor so penny-stock noise never reaches the deep-dive layer.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".cache" / "tradingagents_scanner"
SP500_CACHE = CACHE_DIR / "sp500.csv"
WIKI_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def _normalize(ticker: str) -> str:
    """yfinance spelling: BRK.B -> BRK-B, strip whitespace, uppercase."""
    return ticker.strip().upper().replace(".", "-")


def _fetch_sp500() -> list[str]:
    """S&P 500 constituents from Wikipedia, cached locally for a week."""
    import time

    import pandas as pd

    if SP500_CACHE.exists() and time.time() - SP500_CACHE.stat().st_mtime < 7 * 86400:
        tickers = [t for t in SP500_CACHE.read_text().split() if t]
        if tickers:
            logger.info("Using cached S&P 500 list (%d tickers)", len(tickers))
            return tickers
    try:
        import requests
        from io import StringIO

        headers = {"User-Agent": "Mozilla/5.0 (compatible; TradingAgents-scanner/1.0)"}
        resp = requests.get(WIKI_SP500_URL, headers=headers, timeout=30)
        resp.raise_for_status()
        tables = pd.read_html(StringIO(resp.text))
        tickers = sorted({_normalize(str(t)) for t in tables[0]["Symbol"]})
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        SP500_CACHE.write_text("\n".join(tickers))
        logger.info("Fetched S&P 500 list (%d tickers)", len(tickers))
        return tickers
    except Exception as exc:  # offline or Wikipedia changed layout
        logger.warning("Could not fetch S&P 500 list: %s", exc)
        if SP500_CACHE.exists():
            return [t for t in SP500_CACHE.read_text().split() if t]
        raise RuntimeError(
            "No cached S&P 500 list and Wikipedia fetch failed; "
            "set SCANNER_UNIVERSE to a file with tickers."
        ) from exc


def get_universe() -> list[str]:
    """Tickers to scan, per SCANNER_UNIVERSE."""
    source = os.environ.get("SCANNER_UNIVERSE", "sp500").strip()
    if source.lower() == "sp500":
        return _fetch_sp500()
    path = Path(source).expanduser()
    if not path.exists():
        raise RuntimeError(f"SCANNER_UNIVERSE file not found: {path}")
    tickers = sorted({_normalize(t) for t in path.read_text().split() if t.strip()})
    logger.info("Using custom universe from %s (%d tickers)", path, len(tickers))
    return tickers
