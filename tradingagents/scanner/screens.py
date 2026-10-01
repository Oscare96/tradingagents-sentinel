"""StocksToTrade-style pre-built screens over the scan universe.

Each screen is a cheap, rule-based filter on Yahoo Finance data -- the same
job StocksToTrade's real-time scanners do: surface *candidates* for review,
not confirmed setups. Screens implemented:

- ``percent_gainers``: biggest intraday % movers (their "percent gainers" scan)
- ``unusual_volume``: volume running far ahead of its 20-day pace (their
  "high relative volume" scan)
- ``gap_ups``: stocks gapping up at the open
- ``new_highs``: trading at/near the 52-week high (their "new highs" scan)
- ``rsi_extreme``: RSI(14) overbought (>=70, momentum) or oversold (<=30,
  potential bounce) -- both are setups their traders watch

Every candidate must clear the liquidity floor (min price and min intraday
dollar volume) so illiquid names never reach the LLM deep-dive layer.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

MIN_PRICE = float(os.environ.get("SCANNER_MIN_PRICE", "2.0"))
MIN_DOLLAR_VOLUME = float(os.environ.get("SCANNER_MIN_DOLLAR_VOLUME", "1_000_000"))

GAIN_PCT = float(os.environ.get("SCANNER_GAIN_PCT", "3.0"))
REL_VOL = float(os.environ.get("SCANNER_REL_VOL", "2.0"))
GAP_PCT = float(os.environ.get("SCANNER_GAP_PCT", "2.0"))
HIGH_LOOKBACK_PCT = float(os.environ.get("SCANNER_HIGH_PROXIMITY_PCT", "95.0"))


@dataclass
class Candidate:
    ticker: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    def add(self, points: float, reason: str, **facts):
        self.score += points
        self.reasons.append(reason)
        self.facts.update(facts)


def _batch_download(tickers: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One batched daily pull and one batched intraday pull for the universe.

    Daily bars only change once a day, so they are cached on disk per date --
    without this, every 15-minute cycle would re-download a year of daily data
    for 500 tickers (~3.5 min) and the cycle could never keep up.
    """
    from tradingagents.scanner.universe import CACHE_DIR
    from datetime import datetime
    import hashlib

    import pytz

    today = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
    uni_hash = hashlib.md5(",".join(sorted(tickers)).encode()).hexdigest()[:8]
    daily_cache = CACHE_DIR / f"daily-{today}-{uni_hash}.pkl"
    if daily_cache.exists():
        logger.info("Using cached daily bars for %s", today)
        daily = pd.read_pickle(daily_cache)
    else:
        logger.info("Downloading daily bars for %d tickers", len(tickers))
        daily = yf.download(
            tickers, period="1y", interval="1d", auto_adjust=True,
            progress=False, threads=True,
        )
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        daily.to_pickle(daily_cache)
    logger.info("Downloading intraday bars for %d tickers", len(tickers))
    intraday = yf.download(
        tickers, period="5d", interval="15m", auto_adjust=True,
        progress=False, threads=True,
    )
    return daily, intraday


def _px(frame: pd.DataFrame, ticker: str, field: str) -> pd.Series:
    """One ticker's series out of a multi-ticker yfinance frame."""
    try:
        s = frame[(field, ticker)]
    except KeyError:
        s = frame[field]  # single-ticker frame: flat columns
    return pd.Series(s).dropna()


def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    return float(100 - 100 / (1 + rs.iloc[-1])) if pd.notna(rs.iloc[-1]) else 50.0


def run_screens(tickers: list[str]) -> list[Candidate]:
    """Run every pre-built screen; return scored, ranked candidates."""
    daily, intraday = _batch_download(tickers)
    cands: dict[str, Candidate] = {}

    def cand(t: str) -> Candidate:
        return cands.setdefault(t, Candidate(ticker=t))

    for t in tickers:
        try:
            d_close = _px(daily, t, "Close")
            d_vol = _px(daily, t, "Volume")
            d_open = _px(daily, t, "Open")
            i_close = _px(intraday, t, "Close")
            i_vol = _px(intraday, t, "Volume")
            if len(d_close) < 25 or i_close.empty:
                continue

            prev_close = float(d_close.iloc[-2])
            last = float(i_close.iloc[-1])
            if last < MIN_PRICE:
                continue

            # Today's session so far (intraday bars stamped today, ET).
            today = i_close.index[-1].date()
            sess = i_close[i_close.index.date == today]
            sess_vol = i_vol[i_vol.index.date == today]
            day_volume = float(sess_vol.sum()) if not sess_vol.empty else 0.0
            dollar_vol = day_volume * last
            if dollar_vol < MIN_DOLLAR_VOLUME:
                continue

            day_open = float(sess.iloc[0]) if not sess.empty else float(d_open.iloc[-1])
            pct_change = (last - prev_close) / prev_close * 100
            gap_pct = (day_open - prev_close) / prev_close * 100

            # Relative volume: today's pace vs 20-day average daily volume.
            avg_vol = float(d_vol.iloc[-21:-1].mean())
            elapsed_frac = max(len(sess) / 26.0, 0.15)  # 26 fifteen-min bars/session
            rel_vol = (day_volume / elapsed_frac) / avg_vol if avg_vol > 0 else 0.0

            hi_52w = float(d_close.tail(252).max())
            proximity = last / hi_52w * 100 if hi_52w > 0 else 0.0
            rsi = _rsi(d_close)

            c = cand(t)
            c.facts.update(
                last=round(last, 2), pct_change=round(pct_change, 2),
                rel_vol=round(rel_vol, 2), gap_pct=round(gap_pct, 2),
                rsi=round(rsi, 1), dollar_vol=int(dollar_vol),
            )

            if pct_change >= GAIN_PCT:
                c.add(3.0 + pct_change / 10, f"up {pct_change:.1f}% intraday")
            if rel_vol >= REL_VOL:
                c.add(2.0 + rel_vol / 4, f"relative volume {rel_vol:.1f}x")
            if gap_pct >= GAP_PCT:
                c.add(2.0, f"gapped up {gap_pct:.1f}%")
            if proximity >= HIGH_LOOKBACK_PCT:
                c.add(1.5, f"at {proximity:.0f}% of 52-week high")
            if rsi >= 70:
                c.add(1.0, f"RSI {rsi:.0f} overbought momentum")
            elif rsi <= 30:
                c.add(1.0, f"RSI {rsi:.0f} oversold bounce setup")
        except Exception as exc:
            logger.debug("Screen skipped %s: %s", t, exc)

    ranked = sorted(
        (c for c in cands.values() if c.score > 0),
        key=lambda c: c.score, reverse=True,
    )
    logger.info("%d candidates passed the screens", len(ranked))
    return ranked
