"""Execution ledger: append-only JSONL log plus restart-recovery state.

- Every decision lands in ``reports/execution/YYYY-MM-DD.jsonl`` as one JSON
  object per line: ``{"ts": ..., "event": ..., ...}``.
- ``state.json`` keeps the small facts a restart needs: current trading
  date, day-start equity, new positions opened today, symbols this trader
  opened, and the consecutive-rejection counter.
- A missing state file means "fresh start". An *unreadable* state file
  raises StateCorruptError: the trader must reconstruct limits from broker
  history, never silently reset them.
- The kill-switch flag is a separate empty file; the trader refuses new
  entries while it exists (but keeps retrying emergency cleanup), and only
  a human removes it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class StateCorruptError(Exception):
    """state.json exists but cannot be parsed -- limits must be rebuilt."""


class Ledger:
    def __init__(self, root: str | Path | None = None) -> None:
        from tradingagents.execution import config as cfg
        self.root = Path(root) if root else Path(cfg.LEDGER_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- events ---------------------------------------------------------
    def _day_file(self, date_str: str) -> Path:
        return self.root / f"{date_str}.jsonl"

    def record(self, date_str: str, event: str, **fields) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        with self._day_file(date_str).open("a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    def read_day(self, date_str: str) -> list:
        path = self._day_file(date_str)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()
                if line.strip()]

    def fills(self, date_str: str) -> list:
        """Fill events recorded today (entry fills, partial fills, and
        exit-leg fills)."""
        return [e for e in self.read_day(date_str)
                if e.get("event") in ("fill", "partial_fill", "exit_fill")]

    def realized_pnl(self, date_str: str) -> dict:
        """Per-symbol realized P&L from today's fills, FIFO matched.

        Buy fills open lots; sell fills close them oldest-first. Symbols
        with open lots contribute nothing yet (their P&L is unrealized and
        comes from broker quotes, not this ledger).
        """
        lots: dict[str, list] = {}
        realized: dict[str, float] = {}
        for f in sorted(self.fills(date_str), key=lambda e: e.get("ts", "")):
            sym, side = f["symbol"], f["side"]
            qty, price = float(f["qty"]), float(f["price"])
            if side == "buy":
                lots.setdefault(sym, []).append([qty, price])
            else:
                remaining = qty
                while remaining > 0 and lots.get(sym):
                    lot_qty, lot_price = lots[sym][0]
                    close_qty = min(remaining, lot_qty)
                    realized[sym] = realized.get(sym, 0.0) + \
                        close_qty * (price - lot_price)
                    lot_qty -= close_qty
                    remaining -= close_qty
                    if lot_qty <= 0:
                        lots[sym].pop(0)
                    else:
                        lots[sym][0][0] = lot_qty
                if remaining > 0:
                    logger.warning("Sell fill without a matching buy lot: %s", f)
        return realized

    # -- state -----------------------------------------------------------
    def _state_file(self) -> Path:
        return self.root / "state.json"

    def load_state(self) -> dict:
        path = self._state_file()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise StateCorruptError(f"state.json unreadable: {exc}") from exc

    def save_state(self, state: dict) -> None:
        self._state_file().write_text(json.dumps(state, indent=2))

    # -- kill flag --------------------------------------------------------
    def _kill_file(self) -> Path:
        from tradingagents.execution import config as cfg
        return self.root / cfg.KILL_FLAG_NAME

    def kill_engaged(self) -> bool:
        return self._kill_file().exists()

    def engage_kill(self, date_str: str, reason: str) -> None:
        self._kill_file().write_text(
            f"Engaged {datetime.now(timezone.utc).isoformat()}: {reason}\n")
        self.record(date_str, "kill_switch_engaged", reason=reason)
        logger.warning("KILL SWITCH ENGAGED: %s", reason)
