"""Sentinel execution package: deterministic Alpaca paper trading.

Paper-only. No LLM output enters the order path. See SPEC.md for the
contract this package enforces.
"""

from tradingagents.execution import broker, config, ledger, risk, strategy, trader

__all__ = ["broker", "config", "ledger", "risk", "strategy", "trader"]
