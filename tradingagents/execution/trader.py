"""Trading-cycle orchestrator: reconcile -> risk-check -> submit -> ledger.

One call = one cycle. The caller (scheduler, later) supplies the latest
scan result. Every branch is logged to the ledger; anything unexpected
fails closed (no order) rather than guessing.
"""

from __future__ import annotations

import logging
from datetime import datetime
from dataclasses import replace

from tradingagents.execution import config as cfg
from tradingagents.execution.broker import AlpacaPaperBroker, BrokerError
from tradingagents.execution.ledger import Ledger
from tradingagents.execution.risk import (
    Position, RiskState, bracket_prices, find_orphans,
    kill_switch_tripped, validate_intent,
)
from tradingagents.execution import strategy as strat
from tradingagents.scanner.market_calendar import ET, market_is_open

logger = logging.getLogger(__name__)


def _et_time(hhmm: str):
    h, m = hhmm.split(":")
    return int(h), int(m)


def _at_or_after(now: datetime, hhmm: str) -> bool:
    h, m = _et_time(hhmm)
    return (now.hour, now.minute) >= (h, m)


def run_trading_cycle(scan_result: dict | None,
                      broker: AlpacaPaperBroker,
                      ledger: Ledger,
                      now: datetime | None = None) -> dict:
    """Run one paper-trading cycle. Returns a summary dict."""
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = ET.localize(now)
    today = now.strftime("%Y-%m-%d")
    summary: dict = {"date": today, "actions": []}

    def note(action: str, **fields):
        summary["actions"].append({"action": action, **fields})
        ledger.record(today, action, **fields)

    # 0. Kill flag from a previous trip blocks everything.
    if ledger.kill_engaged():
        note("cycle_blocked", reason="kill switch engaged (manual reset required)")
        return summary

    # 1. Broker state -- any failure here fails the whole cycle closed.
    try:
        account = broker.connect()
        positions_raw = broker.get_positions()
        open_orders = broker.get_open_orders()
    except BrokerError as exc:
        note("cycle_aborted", reason=f"broker unreachable: {exc}")
        return summary

    equity = float(account.get("equity") or 0)
    buying_power = float(account.get("buying_power") or 0)
    broker_positions = {
        p["symbol"]: Position(p["symbol"], float(p["qty"]),
                              float(p["market_value"]))
        for p in positions_raw
    }
    open_symbols = {o["symbol"] for o in open_orders}
    open_client_ids = {o.get("client_order_id") for o in open_orders
                       if o.get("client_order_id")}

    # 2. Day rollover.
    state = ledger.load_state()
    if state.get("date") != today:
        state = {"date": today, "day_start_equity": equity,
                 "new_positions_today": 0,
                 "opened_symbols": state.get("opened_symbols", [])}
        note("day_rollover", day_start_equity=equity)
    day_start = float(state.get("day_start_equity") or equity)

    # 3. Daily-loss kill switch (checked before anything else trades).
    if kill_switch_tripped(equity, day_start, cfg.DAILY_LOSS_LIMIT_PCT):
        try:
            broker.cancel_all_orders()
            broker.close_all_positions()
            note("kill_switch_positions_closed", equity=equity,
                 day_start_equity=day_start)
        except BrokerError as exc:
            note("kill_switch_close_failed", reason=str(exc))
        ledger.engage_kill(
            today,
            f"daily loss limit hit: equity {equity:,.2f} vs "
            f"day start {day_start:,.2f}")
        return summary

    managed = set(state.get("opened_symbols", [])) & set(broker_positions)
    orphans = find_orphans(set(broker_positions), set(state.get("opened_symbols", [])))
    if orphans:
        note("orphan_positions", symbols=sorted(orphans),
             detail="entries blocked until resolved")

    # 4. End-of-day flatten (v1 holds nothing overnight).
    if _at_or_after(now, cfg.EOD_FLATTEN_ET) and broker_positions:
        try:
            broker.cancel_all_orders()
            broker.close_all_positions()
            note("eod_flatten", symbols=sorted(broker_positions))
        except BrokerError as exc:
            note("eod_flatten_failed", reason=str(exc))
        state["opened_symbols"] = []
        ledger.save_state(state)
        return summary

    # 5. Entries.
    risk_state = RiskState(
        equity=equity, buying_power=buying_power,
        day_start_equity=day_start,
        positions=tuple(broker_positions.values()),
        open_order_symbols=frozenset(open_symbols),
        open_client_order_ids=frozenset(open_client_ids),
        new_positions_today=int(state.get("new_positions_today", 0)),
        kill_engaged=False,
        market_open=market_is_open(now),
    )

    entries_allowed = (
        scan_result is not None
        and scan_result.get("trade_date") == today
        and not orphans
        and risk_state.market_open
        and _at_or_after(now, cfg.ENTRY_AFTER_ET)
        and risk_state.new_positions_today < cfg.MAX_NEW_POSITIONS_PER_DAY
    )
    if scan_result is not None and scan_result.get("trade_date") != today:
        note("entries_skipped", reason="scan is stale "
             f"(trade_date={scan_result.get('trade_date')})")
    elif orphans:
        note("entries_skipped", reason="orphan positions unresolved")
    elif entries_allowed:
        intents = strat.generate_intents(
            scan_result.get("watchlist", []), equity,
            set(broker_positions), open_symbols, today,
            max_new=cfg.MAX_NEW_POSITIONS_PER_DAY - risk_state.new_positions_today,
            start_n=risk_state.new_positions_today + 1,
        )
        for proto in intents:
            try:
                quote = broker.get_latest_ask(proto.symbol)
            except BrokerError as exc:
                note("intent_rejected", symbol=proto.symbol,
                     reasons=[f"no fresh quote: {exc}"])
                continue
            intent = replace(proto, quote_price=quote)
            reasons = validate_intent(
                intent, risk_state,
                expected_strategy_version=cfg.STRATEGY_VERSION,
                max_position_pct=cfg.MAX_POSITION_PCT,
                max_concurrent_positions=cfg.MAX_CONCURRENT_POSITIONS,
                max_gross_exposure_pct=cfg.MAX_GROSS_EXPOSURE_PCT,
                max_new_positions_per_day=cfg.MAX_NEW_POSITIONS_PER_DAY,
                max_quote_drift_pct=cfg.MAX_QUOTE_DRIFT_PCT,
            )
            if reasons:
                note("intent_rejected", symbol=intent.symbol, reasons=reasons)
                continue
            stop, target = bracket_prices(intent.reference_price,
                                          cfg.STOP_LOSS_PCT, cfg.TAKE_PROFIT_PCT)
            try:
                order = broker.submit_bracket_order(
                    intent.symbol, intent.qty, intent.client_order_id,
                    stop, target, side=intent.side)
            except BrokerError as exc:
                # Never retry blindly: reconcile next cycle via open orders.
                note("order_failed", symbol=intent.symbol,
                     client_order_id=intent.client_order_id, reason=str(exc))
                continue
            note("order_submitted", symbol=intent.symbol, qty=intent.qty,
                 reference_price=intent.reference_price, quote_price=quote,
                 stop_price=stop, take_profit_price=target,
                 client_order_id=intent.client_order_id,
                 broker_order_id=order.get("id"))
            state["new_positions_today"] = state.get("new_positions_today", 0) + 1
            opened = set(state.get("opened_symbols", []))
            opened.add(intent.symbol)
            state["opened_symbols"] = sorted(opened)

    ledger.save_state(state)
    summary["equity"] = equity
    summary["positions"] = sorted(broker_positions)
    return summary
