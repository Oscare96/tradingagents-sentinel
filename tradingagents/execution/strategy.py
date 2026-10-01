"""Reference strategy v1: deterministic trade intents from scan candidates.

EXPERIMENTAL AND UNVALIDATED. Nothing here is claimed to be profitable;
this exists so the paper-validation loop has a fixed, auditable rule to
measure. Strategy changes are material: they go through the ChatGPT
sparring protocol and need Oscar's decision before any autonomous schedule
uses them.

Pure function: ranked candidates + account facts -> intents. No LLM output
ever enters this path.
"""

from __future__ import annotations

from tradingagents.execution import config as cfg
from tradingagents.execution.risk import Intent, size_position


def build_client_order_id(date_str: str, symbol: str, side: str,
                          strategy_version: str, n: int) -> str:
    """Idempotent client order id. Same inputs -> same id, always."""
    version = strategy_version.replace("-", "").replace("_", "").lower()
    return f"sentinel-{date_str}-{symbol}-{side}-{version}-{n}"


def generate_intents(candidates: list,
                     equity: float,
                     held_symbols: set,
                     ordered_symbols: set,
                     date_str: str,
                     max_new: int = cfg.MAX_NEW_POSITIONS_PER_DAY,
                     start_n: int = 1) -> list:
    """Build entry intents from ranked scan candidates.

    `candidates` are the scanner's watchlist dicts (``ticker``, ``score``,
    and ``facts.last``). Returns at most `max_new` intents, best score
    first. One intent per symbol, never for held/ordered symbols.
    """
    intents: list = []
    seen: set = set()
    for c in sorted(candidates, key=lambda c: c.get("score", 0), reverse=True):
        if len(intents) >= max_new:
            break
        symbol = c.get("ticker")
        score = c.get("score", 0)
        facts = c.get("facts") or {}
        price = facts.get("last")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        if score < cfg.ENTRY_MIN_SCORE:
            continue  # candidates are score-sorted; nothing below can pass
        if symbol in held_symbols or symbol in ordered_symbols:
            continue
        if not price or price <= 0:
            continue
        qty = size_position(equity, price, cfg.TARGET_POSITION_PCT)
        if qty < 1:
            continue
        n = start_n + len(intents)
        intents.append(Intent(
            symbol=symbol,
            side="buy",
            qty=qty,
            reference_price=float(price),
            quote_price=float(price),  # refreshed by the trader before submit
            strategy_version=cfg.STRATEGY_VERSION,
            client_order_id=build_client_order_id(
                date_str, symbol, "buy", cfg.STRATEGY_VERSION, n),
            price_asof=facts.get("price_asof"),
        ))
    return intents
