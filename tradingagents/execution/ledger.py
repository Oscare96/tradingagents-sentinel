"""Execution ledger: append-only JSONL log plus restart-recovery state.

- Every decision lands in ``reports/execution/YYYY-MM-DD.jsonl`` as one JSON
  object per line: ``{"ts": ..., "event": ..., ...}``.
- ``state.json`` keeps the small facts a restart needs: current trading
  date, day-start equity, new positions opened today, and symbols this
  trader opened (for orphan detection).
- The kill-switch flag is a separate empty file; the trader refuses to run
  while it exists, and only a human removes it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


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

    # -- state -----------------------------------------------------------
    def _state_file(self) -> Path:
        return self.root / "state.json"

    def load_state(self) -> dict:
        path = self._state_file()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

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
