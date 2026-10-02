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
    return render_template(
        "settings.html",
        keys=keystore.key_status(),
        llm_provider=keystore.get_llm_provider(),
    )


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
    allowed = {k: data.get(k, "") for k in keystore.KEY_MAP}
    keystore.save_keys(allowed)
    provider = data.get("llm_provider")
    if provider:
        try:
            keystore.set_llm_provider(provider)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify(
        {
            "ok": True,
            "keys": keystore.key_status(),
            "llm_provider": keystore.get_llm_provider(),
        }
    )


@app.post("/api/llm/test")
def api_llm_test():
    """Verify the selected deep-dive LLM provider is reachable.

    Lists models via the provider's /v1/models endpoint -- no tokens are
    spent. Never returns key material: only the provider name, a model count,
    and a few model ids on success, or a safe error message on failure.
    """
    import urllib.request

    provider = keystore.get_llm_provider()
    if provider == "freellmapi":
        base = os.environ.get("FREELLMAPI_BASE_URL", "http://localhost:3001/v1").rstrip("/")
        key = os.environ.get("FREELLMAPI_API_KEY", "")
    elif provider == "openai":
        base = "https://api.openai.com/v1"
        key = os.environ.get("OPENAI_API_KEY", "")
    else:
        return jsonify({"ok": False, "error": f"unsupported provider '{provider}'"})
    if not key:
        return jsonify(
            {"ok": False, "error": f"no API key stored for provider '{provider}' -- save one first"}
        )
    try:
        req = urllib.request.Request(
            f"{base}/models", headers={"Authorization": f"Bearer {key}"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # network, auth, or bad response -- report safely
        logger.warning("LLM connection test failed for %s: %s", provider, exc)
        return jsonify({"ok": False, "error": f"{provider}: connection failed ({exc})"})
    models = payload.get("data") or []
    ids = [m.get("id", "?") for m in models if isinstance(m, dict)][:5]
    return jsonify(
        {
            "ok": True,
            "provider": provider,
            "endpoint": base,
            "model_count": len(models),
            "sample_models": ids,
        }
    )


@app.post("/api/keys/test")
def api_keys_test():
    """Verify the stored Alpaca keys against the PAPER endpoint.

    Never returns key material: only a masked account number, equity and
    buying power on success, or a safe error message on failure.
    """
    try:
        from tradingagents.execution.broker import AlpacaPaperBroker, BrokerError
    except ImportError as exc:
        return jsonify({"ok": False, "error": f"execution layer unavailable: {exc}"})
    try:
        account = AlpacaPaperBroker().connect()
    except BrokerError as exc:
        logger.warning("Paper connection test failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)})
    num = str(account.get("account_number", ""))
    return jsonify(
        {
            "ok": True,
            "account": "****" + num[-4:] if num else "unknown",
            "equity": account.get("equity"),
            "buying_power": account.get("buying_power"),
            "trading_blocked": bool(account.get("trading_blocked")),
        }
    )


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
