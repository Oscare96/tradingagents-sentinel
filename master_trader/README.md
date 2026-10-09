# Master Trader — AI research and paper execution

Independent application inside Sentinel, on branch `ai-master-trader`. Existing
Sentinel strategies and execution modules are not imported or replaced.

## What runs

- Responsive, authenticated command center with overview, opportunities, positions,
  broker orders, research/news, journals, evaluation, versions, controls, activity.
- Separate research and position-monitor threads, with separate database leases.
- Alpaca stock/crypto snapshots, previous closes and news. Bounded options discovery
  on configured underlyings (first page, 7–45 days to expiry, within 5% of underlying
  price, four contracts per underlying selected by spread). Explicit OCC contracts
  can override discovery. Held instruments remain monitored outside discovery filters.
- OpenAI Responses API with strict structured proposals. Every candidate must receive
  exactly one classification. Refusals, incomplete responses and invented evidence
  fail closed. Missing providers never trigger demo fallback.
- Immutable decision and evidence records, fingerprinted model/prompt versions,
  independent risk calculation, extra cost assumptions and capital/exposure limits.
- Quote-based multiasset simulator; separate champion, challenger, entry-reference,
  pass-reference and random-reference ledgers. Reference exits use a fixed 24-hour
  horizon, 2% stop, 4% target. They are **diagnostics**, not confirmatory significance tests.
- Optional Alpaca **paper** execution: stock limit-entry brackets; crypto buys and
  bought calls/puts with software-monitored exits. No naked selling or crypto shorts.
- Durable order intents saved before submission, unique client IDs, reconciliation
  after network ambiguity/restart, no automatic POST retry. Unresolved intents block
  new broker entries and appear in the dashboard.
- UTC daily loss checks, cumulative experiment loss checks, pause entries, request
  cancellation of unfilled entries, request closure of owned positions, journal export.

**No live broker endpoint exists.** Neither a dashboard action nor an environment
variable can turn this release into a live-money trader.

## Replit deployment

Import `Oscare96/tradingagents-sentinel`, select branch `ai-master-trader`, then use
the repository's `.replit` configuration. Deploy as a **Reserved VM** with one worker;
autoscaling or request-driven shutdown is unsuitable for this position monitor.

Build: `python -m pip install -r master_trader/requirements.txt`

Run: `python -m master_trader.run`

Configure Replit Secrets (see `.env.example`):

| Secret | Purpose |
|---|---|
| `DASHBOARD_TOKEN` | Long random access token; entered in the dashboard. |
| `MASTER_DATABASE_URL` | Persistent PostgreSQL URL. Reuse Replit's `DATABASE_URL` value. |
| `OPENAI_API_KEY` | OpenAI API credential; ChatGPT subscription is not an API credential. |
| `OPENAI_MODEL` | Exact model identifier available to your API account; no guessed default. |
| `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` | Dedicated Alpaca paper account/data credentials. |
| `EXECUTION_BACKEND` | `simulation` initially; `alpaca_paper` for broker paper execution. |
| `STOCK_DATA_FEED` | `iex` default; select `sip` only with appropriate entitlement. |
| `OPTION_DATA_FEED` | `opra`; permission failures remain visible, no silent indicative substitution. |
| `MASTER_DEMO` | `false` for real research; `true` only for synthetic interface testing. |

In Risk & Controls, replace paper example capital/loss limits with the intended
experiment limits. Start in **watchlist / research**, verify quotes, news, AI output,
paper-account approval and activity, then choose **auto / enabled** for paper trading.
Bought options are sized against **full premium risk**; a small paper risk budget
may correctly reject every options contract.

The deployment launcher requires PostgreSQL when `REPLIT_DEPLOYMENT` is set. Always
configure PostgreSQL explicitly even if the platform doesn't expose that variable.
SQLite is for local development, not durable deployed trading records.

## Local testing

From repository root:

```bash
python -m pip install -r master_trader/requirements.txt
python -m pytest -c master_trader/pytest.ini master_trader/tests
```

For synthetic preview, set `MASTER_DEMO=true`, a `DASHBOARD_TOKEN`, and a local
`MASTER_DATABASE_URL=sqlite:///master-trader-preview.db`, then run the launch command.
No provider secrets are needed. Demo prices are fixed, decisions are scripted, and
all pages label the data synthetic. Demo cannot call AI or the paper broker.

## Operational behavior

- Monitor defaults to 30 seconds; research defaults to 15 minutes. Off-hours research
  can use previous closes; execution requires fresh quotes and the broker's open clock.
- Pausing entries continues exits. It does **not** cancel pending entry orders.
  Use Cancel unfilled entries separately. Partially filled entries are not canceled
  automatically, because canceling their brackets can remove protection.
- Closing positions pauses entries and queues closure when quotes and market status
  permit. For bracket positions, cancellation is reconciled before a close order.
- Crypto/options protection requires the running monitor. These exits are paper-only;
  unfilled limit orders and outages can delay closure. Stops do not guarantee prices.
- Use a dedicated paper account. Foreign positions or open orders prevent new entries.
  Broker equity includes the whole account; simulated books have independent capital.
- An unknown order blocks new broker entries until reconciled. There is deliberately
  no dashboard override that blindly submits it again. Investigate the broker/client ID.
- Never lose or reset the database to clear a loss limit or order ambiguity. Back up
  PostgreSQL and export the evidence journal. Destroying history invalidates evaluation.
- Provider secrets never enter model inputs or exports. Browser token stays in tab
  memory. Account API routes require bearer authentication and have no permissive CORS.
- API errors are sanitized, but URLs/headlines are untrusted external data. Broker
  credentials remain in the backend; the model only returns validated proposals.

## What is not yet demonstrated or implemented

- No profitable edge has been established. Real provider integration has to be
  smoke-tested with owner credentials; automated tests use fixtures and mocked calls.
- No live execution, automatic statistical approval, automatic challenger promotion,
  multi-leg spreads, assignment reconciliation or full options lifecycle coverage.
- The challenger runs only if `CHALLENGER_INSTRUCTIONS` is configured. It shares captured
  research input, but has a separate ledger. Prompt changes create new version IDs.
- No full-market discovery, economic/earnings calendar API or social sentiment feed.
  AI sentiment comes from supplied news. Paper short-borrow costs are not modeled.
- Reference participation is affected by capital/position constraints. Results are
  descriptive and must not be presented as a randomized causal experiment.
- A production evaluation registry, scheduled statistical checkpoints, training/test
  separation and evidence-based promotion are required before any live integration.

## Builder handoff

The test suite covers malformed/stale data, fabricated evidence, minimum options
size, loss/exposure gates, authentication, persistence, idempotent events, independent
leases, demo isolation, paper broker intents and restart accounting.

Next integration checks: validate stock/crypto/options quote shapes and feed permissions;
reconcile a paper bracket, partial fill and canceled entry; simulate an ambiguous order
response and restart; verify near-expiration exits and PostgreSQL concurrency; measure
real paper slippage; register the first falsifiable edge experiment. Do not infer success
from a green test suite or a synthetic dashboard.
