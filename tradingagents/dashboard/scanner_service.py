"""Background scanner service: runs the 15-minute scan loop in a thread.

State (running on/off + interval) persists in
``~/.config/tradingagents-scanner/state.json`` so the dashboard resumes the
scanner automatically after a restart if it was on.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path.home() / ".config" / "tradingagents-scanner" / "state.json"


class ScannerService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.running = False
        self.interval_min = 15
        self.scanning = False
        self.last_error: str | None = None
        self._load_state()

    # -- persistence -----------------------------------------------------
    def _load_state(self) -> None:
        try:
            if STATE_FILE.exists():
                s = json.loads(STATE_FILE.read_text())
                self.interval_min = int(s.get("interval_min", 15))
                if s.get("running"):
                    self.start(self.interval_min)
        except Exception as exc:  # corrupt state file: start clean
            logger.warning("Could not load scanner state: %s", exc)

    def _save_state(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(
                json.dumps({"running": self.running, "interval_min": self.interval_min})
            )
        except Exception as exc:
            logger.warning("Could not save scanner state: %s", exc)

    # -- control ---------------------------------------------------------
    def start(self, interval_min: int = 15) -> dict:
        with self._lock:
            if self.running:
                return {"running": True, "interval_min": self.interval_min}
            self.interval_min = max(1, int(interval_min))
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="scanner-loop", daemon=True
            )
            self.running = True
            self.last_error = None
            self._thread.start()
            self._save_state()
            logger.info("Scanner loop started (every %d min)", self.interval_min)
            return {"running": True, "interval_min": self.interval_min}

    def stop(self) -> dict:
        with self._lock:
            self._stop.set()
            self.running = False
            self._save_state()
            logger.info("Scanner loop stopped")
            return {"running": False}

    def run_once(self) -> None:
        """Fire a single scan in the background (does not affect the loop)."""
        t = threading.Thread(target=self._do_scan, name="scanner-once", daemon=True)
        t.start()

    # -- internals -------------------------------------------------------
    def _loop(self) -> None:
        # Run immediately on start, then every interval.
        self._do_scan()
        while not self._stop.wait(self.interval_min * 60):
            self._do_scan()

    def _do_scan(self) -> None:
        from tradingagents.scanner.scan import run_scan

        with self._lock:
            if self.scanning:
                logger.info("Scan already in progress, skipping")
                return
            self.scanning = True
        try:
            run_scan()
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("Scan failed")
        finally:
            with self._lock:
                self.scanning = False
