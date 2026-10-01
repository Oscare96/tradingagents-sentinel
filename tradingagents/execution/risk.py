"""Deterministic risk engine. Pure functions only -- no I/O, no clock.

Every rule in SPEC.md sections 3-7 that can be decided from data lives here
as a testable function. `validate_intent` returns a list of rejection
reasons; an empty list means the intent is approved.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    market_value: float


@dataclass(frozen=True)
class RiskState:
    equity: float
    buying_power: float
    day_start_equity: float
    positions: tuple = ()
    open_order_symbols: frozenset = frozenset()
    open_client_order_ids: frozenset = frozenset()
    new_positions_today: int = 0
    kill_engaged: bool = False
    market_open: bool = False


@dataclass(frozen=True)
class Intent:
    symbol: str
    side: str  # "buy" only in v1
    qty: int
    reference_price: float
    quote_price: float  # fresh quote, checked for drift
    strategy_version: str
    client_order_id: str


def size_position(equity: float, reference_price: float, target_pct: float) -> int:
    """Whole-share quantity for `target_pct` of equity. 0 means 'too small'."""
    if equity <= 0 or reference_price <= 0 or target_pct <= 0:
        return 0
    return int((equity * target_pct) // reference_price)


def bracket_prices(reference_price: float, stop_pct: float,
                   take_profit_pct: float) -> tuple:
    """(stop_price, take_profit_price) for a long bracket entry."""
    stop = round(reference_price * (1 - stop_pct), 2)
    target = round(reference_price * (1 + take_profit_pct), 2)
    return stop, target


def daily_pnl_pct(equity: float, day_start_equity: float) -> float:
    if day_start_equity <= 0:
        return 0.0
    return (equity - day_start_equity) / day_start_equity


def kill_switch_tripped(equity: float, day_start_equity: float,
                        loss_limit_pct: float) -> bool:
    return daily_pnl_pct(equity, day_start_equity) <= -abs(loss_limit_pct)


def gross_exposure_pct(positions: tuple, equity: float) -> float:
    if equity <= 0:
        return 0.0
    return sum(abs(p.market_value) for p in positions) / equity


def validate_intent(intent: Intent, state: RiskState,
                    expected_strategy_version: str,
                    max_position_pct: float,
                    max_concurrent_positions: int,
                    max_gross_exposure_pct: float,
                    max_new_positions_per_day: int,
                    max_quote_drift_pct: float) -> list:
    """Return rejection reasons; empty list = approved."""
    reasons: list = []

    if state.kill_engaged:
        return ["kill switch engaged"]
    if not state.market_open:
        reasons.append("market is closed")
    if intent.side != "buy":
        reasons.append(f"unsupported side {intent.side!r} (v1 is long-only)")
    if intent.qty < 1:
        reasons.append("quantity < 1 share")
    if intent.strategy_version != expected_strategy_version:
        reasons.append(
            f"strategy version {intent.strategy_version!r} != "
            f"expected {expected_strategy_version!r}"
        )
    if any(p.symbol == intent.symbol for p in state.positions):
        reasons.append(f"already hold {intent.symbol}")
    if intent.symbol in state.open_order_symbols:
        reasons.append(f"open order already exists for {intent.symbol}")
    if intent.client_order_id in state.open_client_order_ids:
        reasons.append(f"client_order_id {intent.client_order_id} already submitted")
    if state.new_positions_today >= max_new_positions_per_day:
        reasons.append("daily new-position limit reached")

    if state.equity > 0:
        notional = intent.qty * intent.reference_price
        if notional > state.equity * max_position_pct:
            reasons.append(
                f"position value ${notional:,.2f} exceeds "
                f"{max_position_pct:.0%} of equity"
            )
        if len(state.positions) >= max_concurrent_positions:
            reasons.append("max concurrent positions reached")
        new_exposure = (
            gross_exposure_pct(state.positions, state.equity)
            + notional / state.equity
        )
        if new_exposure > max_gross_exposure_pct:
            reasons.append(
                f"gross exposure would be {new_exposure:.1%} "
                f"(cap {max_gross_exposure_pct:.0%})"
            )
        if notional > state.buying_power:
            reasons.append("insufficient buying power")
    else:
        reasons.append("equity unavailable")

    if intent.reference_price > 0:
        drift = abs(intent.quote_price - intent.reference_price) / intent.reference_price
        if drift > max_quote_drift_pct:
            reasons.append(
                f"quote drifted {drift:.1%} since scan (cap {max_quote_drift_pct:.0%})"
            )
    else:
        reasons.append("reference price missing")

    return reasons


def find_orphans(broker_symbols: set, managed_symbols: set) -> set:
    """Positions the broker holds that this trader did not open."""
    return set(broker_symbols) - set(managed_symbols)
