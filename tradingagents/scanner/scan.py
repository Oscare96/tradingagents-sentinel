"""Scanner orchestrator: screens -> rank -> deep dive -> watchlist report.

One run of ``run_scan()`` is one 15-minute cycle. It is market-hours aware
(America/New_York, Mon-Fri 09:30-16:00): outside market hours the screens run
on daily data in screen-only mode and no LLM budget is spent.

Reports land in ``reports/scanner/<YYYY-MM-DD>/HHMM-scan.{md,json}`` so every
cycle leaves an auditable trail of what was seen and decided.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import pytz

from tradingagents.scanner import deep_dive, screens
from tradingagents.scanner.universe import get_universe

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")
REPORT_ROOT = Path(__file__).resolve().parent.parent.parent / "reports" / "scanner"

_XNYS_CAL = None


def _xnys_calendar():
    """NYSE calendar, lazily built; None when pandas_market_calendars is absent."""
    global _XNYS_CAL
    if _XNYS_CAL is None:
        try:
            import pandas_market_calendars as mcal
        except ImportError:
            return None
        _XNYS_CAL = mcal.get_calendar("XNYS")
    return _XNYS_CAL


def market_is_open(now: datetime | None = None) -> bool:
    """True during the NYSE regular session, exchange holidays excluded."""
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = ET.localize(now)
    if now.weekday() >= 5:
        return False
    cal = _xnys_calendar()
    if cal is None:  # fallback: no holiday data, assume a normal 9:30-16:00 day
        open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
        close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return open_t <= now <= close_t
    sched = cal.schedule(start_date=now.date(), end_date=now.date())
    if sched.empty:  # exchange holiday
        return False
    row = sched.iloc[0]
    return row["market_open"] <= now <= row["market_close"]


def run_scan(top_n: int | None = None, deep_dive_enabled: bool | None = None) -> dict:
    top_n = top_n if top_n is not None else int(os.environ.get("SCANNER_TOP_N", "3"))
    if deep_dive_enabled is None:
        deep_dive_enabled = os.environ.get("SCANNER_DEEP_DIVE", "true").lower() == "true"

    now = datetime.now(ET)
    trade_date = now.strftime("%Y-%m-%d")
    is_open = market_is_open(now)
    logger.info("Scan %s | market open: %s", now.strftime("%H:%M ET"), is_open)

    tickers = get_universe()
    candidates = screens.run_screens(tickers)
    top = candidates[:top_n]

    llm_ok, llm_note = deep_dive.deep_dive_available()
    do_dive = bool(deep_dive_enabled and is_open and llm_ok and top)
    analyses = deep_dive.analyze_tickers([c.ticker for c in top], trade_date) if do_dive else []
    analysis_by_ticker = {a["ticker"]: a for a in analyses}

    watchlist = []
    for c in top:
        entry = {
            "ticker": c.ticker,
            "score": round(c.score, 2),
            "reasons": c.reasons,
            "facts": c.facts,
            "signal": analysis_by_ticker.get(c.ticker, {}).get("signal"),
            "deep_dive": analysis_by_ticker.get(c.ticker, {}).get("ok") is True,
        }
        watchlist.append(entry)

    result = {
        "scanned_at": now.isoformat(),
        "trade_date": trade_date,
        "market_open": is_open,
        "universe_size": len(tickers),
        "candidates_found": len(candidates),
        "deep_dive_ran": do_dive,
        "deep_dive_note": llm_note if not do_dive else "deep dive completed",
        "watchlist": watchlist,
    }
    _write_report(result, now)
    return result


def _write_report(result: dict, now: datetime) -> Path:
    day_dir = REPORT_ROOT / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%H%M")
    (day_dir / f"{stamp}-scan.json").write_text(json.dumps(result, indent=2))

    lines = [
        f"# Market Scan -- {result['scanned_at']}",
        "",
        f"Universe: {result['universe_size']} tickers | "
        f"Candidates: {result['candidates_found']} | "
        f"Market open: {result['market_open']} | "
        f"Deep dive: {'yes' if result['deep_dive_ran'] else 'no (' + result['deep_dive_note'] + ')'}",
        "",
        "## Watchlist",
    ]
    if not result["watchlist"]:
        lines.append("_No candidates passed the screens this cycle._")
    for w in result["watchlist"]:
        f = w["facts"]
        lines.append(
            f"\n### {w['ticker']} -- score {w['score']}"
            + (f" -- signal **{w['signal']}**" if w["signal"] else "")
        )
        lines.append(f"- ${f.get('last')} ({f.get('pct_change')}%), "
                     f"rel vol {f.get('rel_vol')}x, RSI {f.get('rsi')}")
        lines.append("- Screens: " + "; ".join(w["reasons"]))
    md_path = day_dir / f"{stamp}-scan.md"
    md_path.write_text("\n".join(lines) + "\n")
    logger.info("Report written to %s", md_path)
    return md_path
