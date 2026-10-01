# Sentinel Execution Specification — paper trading, deterministic

This document is the contract the code enforces. Every rule below is
implemented in `tradingagents/execution/` and covered by unit tests. If the
code and this document disagree, the document is wrong — fix the document,
then the code. (The one exception: any rule marked **SAFETY** below may only
be relaxed with Oscar's explicit approval.)

No part of this system trades real money. Live trading is not implemented,
not configured, and not possible without code changes.

## 1. Paper-only guarantee (SAFETY)

- The broker adapter talks to exactly one base URL, hard-coded:
  `https://paper-api.alpaca.markets`. There is no configuration knob, env
  var, or argument that can point it at the live endpoint
  (`https://api.alpaca.markets`).
- On connect, the adapter fetches the account and refuses to proceed unless
  the request succeeds against the paper endpoint. Alpaca rejects live keys
  on the paper endpoint, so a live keypair fails closed here.
- Paper trades are explicitly labeled as not validated: nothing in this
  repository claims any strategy is profitable.

## 2. What generates trade intents

- Intents come **only** from `strategy.generate_intents()`, a pure,
  deterministic function of the scanner's ranked candidates. No LLM output,
  score explanation, or chat text ever enters the order path. The deep-dive
  layer remains research-only.
- The reference strategy (`reference-v1`, long-only) is **experimental and
  unvalidated**. Its parameters live in `config.py` in one place and are
  chosen for capital preservation, not profit:
  - Entry: top-scored candidate with `score >= 6.0` (multiple screens must
    fire; this bar is deliberately high and must never be lowered to
    manufacture trades), not already held, no open order for the symbol.
  - Entries are evaluated at most once per trading day, in the first cycle
    at or after 10:00 ET (avoids the opening auction chaos; all inputs are
    the morning scan's facts).
  - One new position per day maximum (slow, auditable paper validation).
- Strategy changes (entry rules, thresholds, sides) are **material**: they
  go through the ChatGPT sparring protocol and need Oscar's decision before
  any autonomous schedule uses them.

## 3. Position sizing and exposure (SAFETY)

All percentages are of current account equity, read from the broker each
cycle (never cached across cycles).

| Parameter | Value |
|---|---|
| Target position value | 2% of equity |
| Max single-position value | 5% of equity |
| Max concurrent positions | 5 |
| Max gross exposure | 25% of equity |
| Sides | Long only |

- Quantity = `floor(target_value / reference_price)`; reference price is the
  candidate's `last` fact from the morning scan, re-checked against a fresh
  quote — if the quote moved more than 2% from the reference, the intent is
  discarded as stale (fail closed, no chase).
- Fractional shares are not used; quantity < 1 share → intent discarded.
- Entries are market orders (paper fills are simulated; the 2% quote-move
  guard is the slippage control for v1).

## 4. Exits — enforced, not suggested (SAFETY)

Every entry is submitted as an **Alpaca bracket order**:

- Entry: market (long).
- Stop-loss: stop order at 4% below the entry fill price.
- Take-profit: limit order at 8% above the entry fill price.

Brackets are held by the broker, so exits survive bot restarts. The 4%/8%
values are a conventional starting point, not a validated edge.

Additionally, the trader closes positions at 15:45 ET each day
(end-of-day flatten; no overnight holds in v1 — removes gap risk from the
validation).

## 5. Daily loss limit and kill switch (SAFETY)

- Day start equity is recorded at the first cycle of each trading day.
- If intraday P&L falls to **-2% of day-start equity**: kill switch trips —
  cancel all open orders, liquidate all positions, and disable new entries
  for the rest of the day. The trip is written to the ledger and requires no
  human action to trigger, but only Oscar re-enables trading (a `kill`
  flag file; the trader refuses to run while it exists).
- The kill switch also trips on: account state unreachable, positions
  irreconcilable with the ledger, or 3 consecutive order rejections.

## 6. Duplicate and conflict prevention (SAFETY)

- Every order carries an idempotent `client_order_id`:
  `sentinel-{YYYYMMDD}-{SYMBOL}-{SIDE}-v1-{n}`, where `n` increments per
  (day, symbol, side). Before submitting, the trader checks the ledger and
  the broker's open orders for the same ID — a match means "already
  submitted", never "submit again".
- Never open a second position in a symbol already held (broker positions
  are the source of truth, checked every cycle).
- Restart recovery: on startup the trader reconciles ledger vs broker
  (positions, open orders, recent fills) before any new order. Anything it
  cannot explain is logged loudly and blocks new entries until resolved.

## 7. Stale-data and calendar rules (SAFETY)

- No entries when the market is closed (checked via the NYSE calendar).
- No entries when the scan's candidates are stale (the screens already drop
  tickers whose latest bar predates the expected session; the trader
  additionally requires the scan's `trade_date` to be today).
- No entries when any of account / positions / clock is unreachable —
  fail closed, log, retry next cycle.

## 8. Ledger and audit

- Every decision is appended to `reports/execution/YYYY-MM-DD.jsonl`:
  intent, risk-check pass/fail with reasons, submitted order (with
  client_order_id), fills, cancels, rejects, stop/target fills, kill-switch
  trips, reconciliation results.
- Realized and unrealized P&L are computed from fills and broker quotes and
  reported per cycle; the dashboard (later) reads the ledger, never the
  other way around.

## 9. What this spec does NOT promise

- No profitability claim. Paper validation exists to *measure*, not to
  prove.
- No shorting, no options, no margin beyond Alpaca's default paper margin,
  no pre/after-hours trading in v1.
- Backtests of recommendations are not backtests of this system. A real
  event-driven simulator is future work, not part of v1.
