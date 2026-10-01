"""Flask dashboard: scanner status/control, watchlist, and API-key settings.

Run with:  python -m tradingagents.dashboard
Then open:  http://127.0.0.1:8787
"""

from __future__ import annotations

import json
import logging
import os

from flask import Flask, jsonify, render_template, request

from tradingagents.dashboard import keys as keystore
from tradingagents.dashboard.scanner_service import ScannerService
from tradingagents.scanner import deep_dive
from tradingagents.scanner.scan import REPORT_ROOT, market_is_open

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
keystore.load_keys_into_env()
service = ScannerService()


def _latest_report() -> dict | None:
    """Newest scan report JSON, or None if no scan has run yet."""
    if not REPORT_ROOT.exists():
        return None
    candidates = sorted(REPORT_ROOT.glob("*/*-scan.json"))
    if not candidates:
        return None
    try:
        return json.loads(candidates[-1].read_text())
    except Exception as exc:
        logger.warning("Could not read report %s: %s", candidates[-1], exc)
        return None


def _status() -> dict:
    report = _latest_report()
    llm_ok, llm_note = deep_dive.deep_dive_available()
    ks = keystore.key_status()
    return {
        "scanner_running": service.running,
        "interval_min": service.interval_min,
        "scanning": service.scanning,
        "market_open": market_is_open(),
        "llm": {"ok": llm_ok, "note": llm_note},
        "alpaca_keys_saved": ks["alpaca_key"]["connected"] and ks["alpaca_secret"]["connected"],
        "last_error": service.last_error,
        "last_scan": (
            {
                "scanned_at": report.get("scanned_at"),
                "universe_size": report.get("universe_size"),
                "candidates_found": report.get("candidates_found"),
                "deep_dive_ran": report.get("deep_dive_ran"),
            }
            if report
            else None
        ),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/settings")
def settings():
    return render_template("settings.html", keys=keystore.key_status())


@app.get("/api/status")
def api_status():
    return jsonify(_status())


@app.get("/api/watchlist")
def api_watchlist():
    report = _latest_report()
    if not report:
        return jsonify({"watchlist": [], "scanned_at": None})
    return jsonify(
        {"watchlist": report.get("watchlist", []), "scanned_at": report.get("scanned_at")}
    )


@app.post("/api/keys")
def api_keys():
    data = request.get_json(force=True, silent=True) or {}
    allowed = {k: data.get(k, "") for k in ("alpaca_key", "alpaca_secret", "openai_key")}
    keystore.save_keys(allowed)
    return jsonify({"ok": True, "keys": keystore.key_status()})


@app.post("/api/scanner/start")
def api_scanner_start():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(service.start(int(data.get("interval_min", 15))))


@app.post("/api/scanner/stop")
def api_scanner_stop():
    return jsonify(service.stop())


@app.post("/api/scan/now")
def api_scan_now():
    service.run_once()
    return jsonify({"ok": True, "message": "scan started in background"})


def main() -> None:
    port = int(os.environ.get("SCANNER_DASHBOARD_PORT", "8787"))
    logger.info("Dashboard at http://127.0.0.1:%d", port)
    app.run(host="127.0.0.1", port=port, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
