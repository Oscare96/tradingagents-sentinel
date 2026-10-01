"""Market scanner package: StocksToTrade-style opportunity discovery for TradingAgents.

Two layers, mirroring how StocksToTrade actually works:

1. **Screen layer (free, fast):** rule-based scans over a ticker universe using
   Yahoo Finance data -- top % gainers, unusual volume, gap-ups, new highs,
   RSI extremes. This is the equivalent of StocksToTrade's pre-built scanners:
   cheap filters that surface *candidates*, not trade signals.
2. **Deep-dive layer (LLM, paid):** the top-ranked candidates are fed into the
   full TradingAgents multi-agent pipeline (analysts -> bull/bear debate ->
   trader -> risk team -> portfolio manager) for a full decision report. This
   is the equivalent of the "Oracle" watchlist: an idea generator with
   suggested levels, for the human to review.

The scanner never places trades. TradingAgents has no broker integration;
its output is a research decision for Oscar to review manually.
"""

from tradingagents.scanner.scan import run_scan

__all__ = ["run_scan"]
