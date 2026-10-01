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

from tradingagents.dataflows.vendors.yahoo.universe import download_batch
from tradingagents.scanner.market_calendar import ET, expected_session_date

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

    today = datetime.now(ET).strftime("%Y-%m-%d")
    uni_hash = hashlib.md5(",".join(sorted(tickers)).encode()).hexdigest()[:8]
    daily_cache = CACHE_DIR / f"daily-{today}-{uni_hash}.pkl"
    if daily_cache.exists():
        logger.info("Using cached daily bars for %s", today)
        daily = pd.read_pickle(daily_cache)
    else:
        logger.info("Downloading daily bars for %d tickers", len(tickers))
        daily = download_batch(tickers, period="1y", interval="1d")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        daily.to_pickle(daily_cache)
    # Intraday is always fresh: a cache here could serve yesterday's session
    # as today's, which is exactly the staleness the screens guard against.
    logger.info("Downloading intraday bars for %d tickers", len(tickers))
    intraday = download_batch(tickers, period="5d", interval="15m")
    return daily, intraday


def _px(frame: pd.DataFrame, ticker: str, field: str) -> pd.Series:
    """One ticker's series out of a multi-ticker yfinance frame."""
    try:
        s = frame[(field, ticker)]
    except KeyError:
        s = frame[field]  # single-ticker frame: flat columns
    return pd.Series(s).dropna()


def _rsi(close: pd.Series, period: int = 14) -> float:
    """Wilder's RSI with standard edge handling.

    A one-sided series used to collapse to 50.0 (average loss of 0 made the
    RS ratio NaN): all-gains must read 100, all-losses 0, flat 50.
    """
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    avg_gain = float(gain.iloc[-1])
    avg_loss = float(loss.iloc[-1])
    if pd.isna(avg_gain) or pd.isna(avg_loss):
        return 50.0
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return float(100 - 100 / (1 + rs))


def _prev_session_close(d_close: pd.Series, session_date) -> float:
    """Close of the last completed daily bar *before* the price session.

    The old code blindly took ``d_close.iloc[-2]``, which is only right when
    the daily frame's final bar is today's still-forming bar. After hours, on
    weekends, or with a pre-market-written daily cache, the frame ends on a
    completed day and ``iloc[-2]`` silently picks the wrong session -- e.g.
    the day before yesterday. Anchoring to the latest intraday bar's session
    date is correct in every case.
    """
    hist = d_close[d_close.index.date < session_date]
    if hist.empty:
        hist = d_close.iloc[:-1] if len(d_close) > 1 else d_close
    return float(hist.iloc[-1])


def _screen_one(
    t: str,
    d_close: pd.Series,
    d_vol: pd.Series,
    d_open: pd.Series,
    i_close: pd.Series,
    i_vol: pd.Series,
    i_open: pd.Series,
) -> Candidate | None:
    """Score one ticker; None when it fails data/liquidity gates or no screen."""
    if len(d_close) < 25 or i_close.empty:
        return None

    last = float(i_close.iloc[-1])
    if last < MIN_PRICE:
        return None

    # The session the latest price belongs to (usually today, ET).
    session_date = i_close.index[-1].date()
    sess = i_close[i_close.index.date == session_date]
    sess_vol = i_vol[i_vol.index.date == session_date]
    sess_open = i_open[i_open.index.date == session_date].dropna()
    day_volume = float(sess_vol.sum()) if not sess_vol.empty else 0.0
    dollar_vol = day_volume * last
    if dollar_vol < MIN_DOLLAR_VOLUME:
        return None

    prev_close = _prev_session_close(d_close, session_date)
    if prev_close <= 0:
        return None
    # Gap is measured from the session's opening print, not the first
    # 15-minute bar's close. When today's opening print is missing the gap is
    # *unavailable* (None): falling back to another session's open produced
    # plausible-looking but wrong numbers (e.g. -16.7% instead of -10%).
    if not sess_open.empty:
        day_open = float(sess_open.iloc[0])
        gap_pct: float | None = (day_open - prev_close) / prev_close * 100
    else:
        gap_pct = None
    pct_change = (last - prev_close) / prev_close * 100

    # Relative volume: today's pace vs the 20 completed sessions before it.
    hist_vol = d_vol[d_vol.index.date < session_date]
    avg_vol = float(hist_vol.iloc[-20:].mean()) if len(hist_vol) else 0.0
    elapsed_frac = max(len(sess) / 26.0, 0.15)  # 26 fifteen-min bars/session
    rel_vol = (day_volume / elapsed_frac) / avg_vol if avg_vol > 0 else 0.0

    hi_52w = float(d_close.tail(252).max())
    proximity = last / hi_52w * 100 if hi_52w > 0 else 0.0
    rsi = _rsi(d_close)

    c = Candidate(ticker=t)
    c.facts.update(
        last=round(last, 2), pct_change=round(pct_change, 2),
        rel_vol=round(rel_vol, 2),
        gap_pct=round(gap_pct, 2) if gap_pct is not None else None,
        rsi=round(rsi, 1), dollar_vol=int(dollar_vol),
        price_asof=i_close.index[-1].isoformat(),
    )

    if pct_change >= GAIN_PCT:
        c.add(3.0 + pct_change / 10, f"up {pct_change:.1f}% intraday")
    if rel_vol >= REL_VOL:
        c.add(2.0 + rel_vol / 4, f"relative volume {rel_vol:.1f}x")
    if gap_pct is not None and gap_pct >= GAP_PCT:
        c.add(2.0, f"gapped up {gap_pct:.1f}%")
    if proximity >= HIGH_LOOKBACK_PCT:
        c.add(1.5, f"at {proximity:.0f}% of 52-week high")
    if rsi >= 70:
        c.add(1.0, f"RSI {rsi:.0f} overbought momentum")
    elif rsi <= 30:
        c.add(1.0, f"RSI {rsi:.0f} oversold bounce setup")
    return c if c.score > 0 else None



def run_screens(
    tickers: list[str], now: "datetime | None" = None
) -> "tuple[list[Candidate], dict]":
    """Run every pre-built screen; return (scored ranked candidates, stats).

    Tickers whose latest bar predates the expected trading session are
    skipped as stale -- without this, a halted ticker or a partial vendor
    outage lets yesterday's prices into today's watchlist. ``stats`` reports
    how many were skipped so the omission is visible, not silent.
    """
    from datetime import datetime

    now = now or datetime.now(ET)
    exp_session = expected_session_date(now)
    daily, intraday = _batch_download(tickers)
    cands: dict[str, Candidate] = {}
    stale = 0

    for t in tickers:
        try:
            d_close = _px(daily, t, "Close")
            d_vol = _px(daily, t, "Volume")
            d_open = _px(daily, t, "Open")
            i_close = _px(intraday, t, "Close")
            i_vol = _px(intraday, t, "Volume")
            i_open = _px(intraday, t, "Open")
            if (
                exp_session is not None
                and not i_close.empty
                and i_close.index[-1].date() < exp_session
            ):
                stale += 1
                logger.debug(
                    "Skipping %s: latest bar %s predates expected session %s",
                    t, i_close.index[-1].date(), exp_session,
                )
                continue
            c = _screen_one(t, d_close, d_vol, d_open, i_close, i_vol, i_open)
            if c is not None:
                cands[t] = c
        except Exception as exc:
            logger.debug("Screen skipped %s: %s", t, exc)

    ranked = sorted(cands.values(), key=lambda c: c.score, reverse=True)
    logger.info(
        "%d candidates passed the screens (%d tickers skipped as stale)",
        len(ranked), stale,
    )
    return ranked, {"stale_skipped": stale}
