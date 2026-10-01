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
        # Generation of the loop thread. Every start()/stop() bumps it; a
        # loop thread whose generation no longer matches exits after its
        # in-flight scan instead of scheduling more work. This is what makes
        # a quick stop -> start restart safe: without it, the old thread can
        # survive on the (cleared) shared stop event and two loops schedule
        # duplicate scans and duplicate LLM deep dives.
        self._generation = 0
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
            self.interval_min = max(1, int(interval_min))
            if self.running:
                # Already looping: just pick up the new interval. The loop
                # re-reads interval_min every cycle, so no restart is needed.
                self._save_state()
                logger.info(
                    "Scanner already running; interval updated to %d min",
                    self.interval_min,
                )
                return {"running": True, "interval_min": self.interval_min}
            self._generation += 1
            gen = self._generation
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, args=(gen,), name="scanner-loop", daemon=True
            )
            self.running = True
            self.last_error = None
            self._thread.start()
            self._save_state()
            logger.info("Scanner loop started (every %d min)", self.interval_min)
            return {"running": True, "interval_min": self.interval_min}

    def stop(self) -> dict:
        with self._lock:
            # Bump the generation so an in-flight loop thread exits after its
            # current scan instead of living on as a duplicate scheduler.
            self._generation += 1
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
    def _loop(self, gen: int) -> None:
        # Run immediately on start, then every interval. The generation check
        # at the top of each cycle guarantees a superseded thread (quick
        # stop -> start while a scan was in flight) exits instead of
        # scheduling duplicate scans.
        while True:
            with self._lock:
                if gen != self._generation or self._stop.is_set():
                    logger.info(
                        "Scanner loop generation %d exiting (current %d)",
                        gen, self._generation,
                    )
                    return
            self._do_scan()
            if self._stop.wait(self.interval_min * 60):
                return

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
