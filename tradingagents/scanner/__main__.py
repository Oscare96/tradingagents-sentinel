"""CLI entrypoint for the 15-minute market scanner.

Run a single scan cycle::

    python -m tradingagents.scanner
    python -m tradingagents.scanner --top-n 5 --screen-only

Schedule every 15 minutes on weekdays (cron)::

    */15 9-16 * * 1-5  cd ~/workspace/studies/TradingAgents && python -m tradingagents.scanner

The scanner itself guards market hours, so stray runs outside 09:30-16:00 ET
degrade to a daily-data screen-only pass instead of spending LLM budget.
"""

from __future__ import annotations

import logging

import typer

app = typer.Typer(add_completion=False)


@app.command()
def main(
    top_n: int = typer.Option(3, help="Candidates sent to the LLM deep dive."),
    screen_only: bool = typer.Option(
        False, help="Skip the LLM deep dive; screens only (free)."
    ),
    quiet: bool = typer.Option(False, help="Less console output."),
):
    logging.basicConfig(
        level=logging.WARNING if quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    from tradingagents.scanner.scan import run_scan

    result = run_scan(top_n=top_n, deep_dive_enabled=not screen_only)

    print(f"\nScan {result['scanned_at']} | universe {result['universe_size']} | "
          f"{result['candidates_found']} candidates | "
          f"deep dive: {'yes' if result['deep_dive_ran'] else 'no'}")
    if not result["deep_dive_ran"]:
        print(f"  ({result['deep_dive_note']})")
    for w in result["watchlist"]:
        sig = f" -> {w['signal']}" if w["signal"] else ""
        print(f"  {w['ticker']}: score {w['score']}{sig} | {'; '.join(w['reasons'])}")
    print()


if __name__ == "__main__":
    app()
