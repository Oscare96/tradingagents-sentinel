"""Trading-cycle orchestrator: reconcile -> risk-check -> submit -> ledger.

One call = one cycle. The caller (scanner scheduler) supplies the latest
scan result. Every branch is logged to the ledger; anything unexpected
fails closed (no order) rather than guessing.

Safety properties (see SPEC.md):
- Entries are blocked past the session-aware EOD cutoff, even with an
  empty account.
- A tripped kill switch keeps retrying emergency cleanup every cycle until
  the broker confirms the account is flat. Entries stay blocked until a
  human clears the flag.
- An unreadable state file never resets limits silently: limits are
  rebuilt from broker history, or entries are blocked.
- Entries require a fresh scan (scanned_at) and fresh candidate prices
  (price_asof).
- Three consecutive order-submission failures trip the kill switch.
- Fills are reconciled every cycle; realized P&L is computed from fills.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta

from tradingagents.execution import config as cfg
from tradingagents.execution.broker import AlpacaPaperBroker, BrokerError
from tradingagents.execution.ledger import Ledger, StateCorruptError
from tradingagents.execution.risk import (
    Position, RiskState, bracket_prices, find_orphans,
    kill_switch_tripped, validate_intent,
)
from tradingagents.execution import strategy as strat
from tradingagents.scanner.market_calendar import (
    ET, market_is_open, session_close,
)

logger = logging.getLogger(__name__)


# -- small helpers ------------------------------------------------------
def _et_time(hhmm: str):
    h, m = hhmm.split(":")
    return int(h), int(m)


def _at_or_after(now: datetime, hhmm: str) -> bool:
    h, m = _et_time(hhmm)
    return (now.hour, now.minute) >= (h, m)


def _parse_ts(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = ET.localize(dt)
    return dt


def _age_min(value, now: datetime) -> float | None:
    dt = _parse_ts(value)
    if dt is None:
        return None
    return (now - dt).total_seconds() / 60


def _eod_cutoff(now: datetime) -> datetime:
    """No new entries at/after this time; flatten everything by it.

    Session-aware: on early-close days the cutoff moves with the actual
    close. Falls back to 16:00 ET when the calendar cannot answer.
    """
    close = session_close(now)
    if close is None:
        close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if close.tzinfo is None:
            close = ET.localize(close)
    return close - timedelta(minutes=cfg.EOD_FLATTEN_MIN_BEFORE_CLOSE)


def _scan_fresh(scan_result: dict | None, now: datetime) -> tuple:
    """(ok, reason): the scan must be recent enough to trade on."""
    if not scan_result:
        return False, "no scan result"
    age = _age_min(scan_result.get("scanned_at"), now)
    if age is None:
        return False, "scan has no usable scanned_at"
    if age > cfg.MAX_SCAN_AGE_MIN:
        return False, f"scan is {age:.0f} min old (cap {cfg.MAX_SCAN_AGE_MIN})"
    return True, ""


def _reconstruct_state(broker: AlpacaPaperBroker, today: str) -> dict:
    """Rebuild trading limits from broker history after state loss.

    Counts today's filled buy orders for the daily entry count and uses
    the portfolio history's base value for day-start equity. Raises
    BrokerError when history is unavailable -- the caller then blocks
    entries instead of guessing.
    """
    orders = broker.get_orders(status="closed", limit=500)
    buys = [o for o in orders
            if o.get("status") == "filled" and o.get("side") == "buy"
            and str(o.get("filled_at", ""))[:10] == today]
    hist = broker.get_portfolio_history(period="1D")
    base = (hist or {}).get("base_value")
    if base is None:
        raise BrokerError("portfolio history has no base_value")
    return {
        "date": today,
        "day_start_equity": float(base),
        "new_positions_today": len(buys),
        "opened_symbols": sorted({o["symbol"] for o in buys if o.get("symbol")}),
        "consecutive_rejections": 0,
        "reconstructed": True,
    }


def _reconcile_fills(broker: AlpacaPaperBroker, ledger: Ledger,
                     state: dict, today: str, note) -> None:
    """Record fills for our orders and exit legs; flag partial fills."""
    try:
        closed = broker.get_orders(status="closed", limit=200)
    except BrokerError as exc:
        note("fill_reconciliation_failed", reason=str(exc))
        return
    submitted: dict = {}
    for e in ledger.read_day(today):
        if e.get("event") == "order_submitted" and e.get("client_order_id"):
            submitted[e["client_order_id"]] = e
    fills = ledger.fills(today)
    filled_cids = {f.get("client_order_id") for f in fills if f.get("client_order_id")}
    seen_broker_ids = {f.get("broker_order_id") for f in fills if f.get("broker_order_id")}
    managed = set(state.get("opened_symbols", []))

    for o in closed:
        if o.get("status") != "filled":
            continue
        cid, oid = o.get("client_order_id"), o.get("id")
        qty = float(o.get("filled_qty") or 0)
        price = float(o.get("filled_avg_price") or 0)
        if cid in submitted and cid not in filled_cids:
            fields = dict(symbol=o["symbol"], side=o.get("side"), qty=qty,
                          price=price, client_order_id=cid, broker_order_id=oid)
            ordered = float(submitted[cid].get("qty") or qty)
            if qty < ordered:
                note("partial_fill", ordered_qty=ordered, **fields)
            else:
                note("fill", **fields)
        elif (o.get("symbol") in managed and o.get("side") == "sell"
                and oid not in seen_broker_ids):
            # Exit-leg fill (stop/target): not our client_order_id, but ours.
            note("exit_fill", symbol=o["symbol"], side="sell", qty=qty,
                 price=price, broker_order_id=oid)


# -- the cycle -----------------------------------------------------------
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

    # 2. State: missing file is a fresh start; a corrupt file is rebuilt
    #    from broker history, never silently reset.
    degraded = False
    try:
        state = ledger.load_state()
    except StateCorruptError as exc:
        note("state_corrupt", reason=str(exc))
        try:
            state = _reconstruct_state(broker, today)
            note("state_reconstructed",
                 new_positions_today=state["new_positions_today"],
                 day_start_equity=state["day_start_equity"])
        except BrokerError as exc2:
            note("entries_blocked",
                 reason=f"state unrecoverable: {exc2}",
                 detail="delete state.json to reset once the cause is fixed")
            degraded = True
            state = {"date": today, "day_start_equity": equity,
                     "new_positions_today": 0, "opened_symbols": [],
                     "consecutive_rejections": 0}
    if not degraded and state.get("date") != today:
        state = {"date": today, "day_start_equity": equity,
                 "new_positions_today": 0,
                 "opened_symbols": state.get("opened_symbols", []),
                 "consecutive_rejections": 0}
        note("day_rollover", day_start_equity=equity)
    day_start = float(state.get("day_start_equity") or equity)

    # 3. Fill reconciliation, before any kill/P&L decision.
    _reconcile_fills(broker, ledger, state, today, note)

    def _trip_kill(reason: str) -> None:
        # Best-effort cleanup now; the kill-engaged branch below retries
        # every cycle until the broker confirms the account is flat.
        try:
            broker.cancel_all_orders()
            broker.close_all_positions()
            note("kill_switch_cleanup_attempt")
        except BrokerError as exc:
            note("kill_switch_close_failed", reason=str(exc))
        ledger.engage_kill(today, reason)

    # 4. Previously tripped kill switch: keep retrying emergency cleanup
    #    until flat. Entries stay blocked; only a human clears the flag.
    if ledger.kill_engaged():
        try:
            broker.cancel_all_orders()
            broker.close_all_positions()
        except BrokerError as exc:
            note("kill_switch_cleanup_failed", reason=str(exc))
            return summary
        try:
            still_open = broker.get_positions()
            still_orders = broker.get_open_orders()
        except BrokerError as exc:
            note("kill_switch_verify_failed", reason=str(exc))
            return summary
        if not still_open and not still_orders:
            note("kill_switch_flat_confirmed",
                 detail="account is flat; entries stay blocked until "
                        "the flag is cleared manually")
        else:
            note("kill_switch_cleanup_pending",
                 positions=[p["symbol"] for p in still_open],
                 open_orders=len(still_orders))
        return summary

    # 5. Kill triggers.
    if kill_switch_tripped(equity, day_start, cfg.DAILY_LOSS_LIMIT_PCT):
        _trip_kill(f"daily loss limit hit: equity {equity:,.2f} vs "
                   f"day start {day_start:,.2f}")
        return summary
    if int(state.get("consecutive_rejections", 0)) >= cfg.MAX_REJECTIONS_BEFORE_KILL:
        _trip_kill(f"{cfg.MAX_REJECTIONS_BEFORE_KILL} consecutive order rejections")
        return summary

    # 6. Orphan detection: positions we did not open block new entries.
    orphans = find_orphans(set(broker_positions),
                           set(state.get("opened_symbols", [])))
    if orphans:
        note("orphan_positions", symbols=sorted(orphans),
             detail="entries blocked until resolved")

    # 7. Session-aware EOD cutoff: flatten and block entries from here on,
    #    even with an empty account (never enter into the close).
    cutoff = _eod_cutoff(now)
    if now >= cutoff:
        if broker_positions or open_orders:
            try:
                broker.cancel_all_orders()
                broker.close_all_positions()
                note("eod_flatten", symbols=sorted(broker_positions),
                     cutoff=cutoff.isoformat())
            except BrokerError as exc:
                note("eod_flatten_failed", reason=str(exc))
                _trip_kill(f"EOD flatten failed: {exc}")
                return summary
        else:
            note("past_eod_cutoff", cutoff=cutoff.isoformat(),
                 detail="entries blocked until next session")
        state["opened_symbols"] = []
        ledger.save_state(state)
        return summary

    # 8. Entries.
    scan_ok, scan_reason = _scan_fresh(scan_result, now)
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
    skip_reasons = []
    if degraded:
        skip_reasons.append("state degraded (unrecoverable)")
    if scan_result is not None and scan_result.get("trade_date") != today:
        skip_reasons.append(f"scan trade_date={scan_result.get('trade_date')}")
    if not scan_ok:
        skip_reasons.append(scan_reason)
    if orphans:
        skip_reasons.append("orphan positions unresolved")
    if not risk_state.market_open:
        skip_reasons.append("market closed")
    if not _at_or_after(now, cfg.ENTRY_AFTER_ET):
        skip_reasons.append(f"before entry window ({cfg.ENTRY_AFTER_ET} ET)")
    if risk_state.new_positions_today >= cfg.MAX_NEW_POSITIONS_PER_DAY:
        skip_reasons.append("daily new-position limit reached")

    entries_allowed = (
        not skip_reasons and scan_result is not None and not degraded
    )
    if not entries_allowed and skip_reasons:
        note("entries_skipped", reasons=skip_reasons)

    if entries_allowed:
        intents = strat.generate_intents(
            scan_result.get("watchlist", []), equity,
            set(broker_positions), open_symbols, today,
            max_new=cfg.MAX_NEW_POSITIONS_PER_DAY - risk_state.new_positions_today,
            start_n=risk_state.new_positions_today + 1,
        )
        for proto in intents:
            age = _age_min(proto.price_asof, now)
            if age is None or age > cfg.MAX_PRICE_AGE_MIN:
                note("intent_rejected", symbol=proto.symbol,
                     reasons=[f"candidate price_asof missing or {age} min old "
                              f"(cap {cfg.MAX_PRICE_AGE_MIN})"])
                continue
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
            # Bracket legs are anchored to the pre-submit quote (seconds
            # old), not the scan reference: for a market order this is the
            # best available proxy for the fill price, with no unprotected
            # window between entry and protection.
            stop, target = bracket_prices(quote,
                                          cfg.STOP_LOSS_PCT, cfg.TAKE_PROFIT_PCT)
            try:
                order = broker.submit_bracket_order(
                    intent.symbol, intent.qty, intent.client_order_id,
                    stop, target, side=intent.side)
            except BrokerError as exc:
                # Never retry a submit blindly: reconcile next cycle via
                # open orders. Count consecutive failures toward the kill
                # switch.
                state["consecutive_rejections"] = \
                    int(state.get("consecutive_rejections", 0)) + 1
                note("order_failed", symbol=intent.symbol,
                     client_order_id=intent.client_order_id, reason=str(exc),
                     consecutive_rejections=state["consecutive_rejections"])
                if state["consecutive_rejections"] >= cfg.MAX_REJECTIONS_BEFORE_KILL:
                    _trip_kill(f"{cfg.MAX_REJECTIONS_BEFORE_KILL} consecutive "
                               "order rejections")
                    ledger.save_state(state)
                    return summary
                continue
            state["consecutive_rejections"] = 0
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
    summary["realized_pnl_today"] = ledger.realized_pnl(today)
    return summary
