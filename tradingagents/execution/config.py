"""Execution configuration: every paper-trading parameter in one place.

Nothing here is validated for profitability. Values are chosen for capital
preservation during paper validation. Changing a SAFETY-marked value in
SPEC.md requires Oscar's explicit approval; changing these numbers without
updating SPEC.md is a bug.
"""

from __future__ import annotations

import os

# --- Broker (paper only; the URL is a constant in broker.py, not here) ---
APCA_KEY_ENV = "APCA_API_KEY_ID"
APCA_SECRET_ENV = "APCA_API_SECRET_KEY"

# --- Strategy: reference-v1 (experimental, unvalidated) ---
STRATEGY_VERSION = "reference-v1"
ENTRY_MIN_SCORE = float(os.environ.get("EXEC_ENTRY_MIN_SCORE", "6.0"))
ENTRY_AFTER_ET = "10:00"          # entries only in cycles at/after 10:00 ET
MAX_NEW_POSITIONS_PER_DAY = 1

# --- Sizing / exposure (fractions of current equity) ---
TARGET_POSITION_PCT = 0.02
MAX_POSITION_PCT = 0.05
MAX_CONCURRENT_POSITIONS = 5
MAX_GROSS_EXPOSURE_PCT = 0.25

# --- Bracket exits (fractions of entry fill price) ---
STOP_LOSS_PCT = 0.04
TAKE_PROFIT_PCT = 0.08

# --- Stale-quote guard: discard intent if the fresh quote moved this far ---
MAX_QUOTE_DRIFT_PCT = 0.02

# --- Kill switch / EOD ---
DAILY_LOSS_LIMIT_PCT = 0.02
EOD_FLATTEN_MIN_BEFORE_CLOSE = 15  # flatten this many minutes before session close
MAX_REJECTIONS_BEFORE_KILL = 3    # consecutive submit failures trip the kill switch

# --- Freshness ---
MAX_SCAN_AGE_MIN = 30             # entries need a scan this fresh (scanned_at)
MAX_PRICE_AGE_MIN = 30            # ...and candidate prices (price_asof) this fresh

# --- Scheduler wiring ---
# The scanner calls run_trading_cycle() after each scan only when this is "1".
# Default off: enabling unattended trading needs Oscar's explicit approval
# after the paper smoke test and risk-parameter review.
EXEC_TRADING_ENABLED = os.environ.get("EXEC_TRADING_ENABLED", "0") == "1"

# --- Ledger / state ---
LEDGER_ROOT = "reports/execution"  # relative to the repo root
KILL_FLAG_NAME = "KILL_SWITCH_ENGAGED"
