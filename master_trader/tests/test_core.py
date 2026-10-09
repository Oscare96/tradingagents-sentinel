from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from master_trader.app import create_app
from master_trader.broker import PaperBroker
from master_trader.engine import Engine, validate_trade
from master_trader.models import Controls, Decision, Quote, utcnow
from master_trader.providers import demo_briefing, demo_snapshot
from master_trader.store import Store


@pytest.fixture
def store(tmp_path):
    return Store("sqlite:///" + str(tmp_path / "test.db"))


@pytest.fixture
def setup():
    snapshot = demo_snapshot()
    quote = Quote.model_validate(snapshot["quotes"][0])
    decision = demo_briefing(snapshot).decisions[0]
    controls = Controls()
    account = {
        "equity": 10000,
        "cash": 10000,
        "gross_exposure": 0,
        "positions": [],
        "missing_marks": [],
        "daily_pnl": 0,
        "total_pnl": 0,
    }
    return quote, decision, controls, account, {quote.source_id}, {"is_open": True}


def test_gate_accepts_valid_supported_trade(setup):
    q, d, c, a, e, clock = setup
    result = validate_trade(d, q, c, a, e, clock)
    assert result["accepted"]
    assert result["qty"] > 0
    assert result["planned_risk"] <= 25


@pytest.mark.parametrize(
    "change,expected",
    [
        ("stale", "stale"),
        ("future", "future"),
        ("invented", "Evidence"),
        ("spread", "Spread"),
        ("closed", "market closed"),
        ("daily", "Daily loss"),
        ("experiment", "experiment loss"),
        ("positions", "Position count"),
        ("liquidity", "liquidity"),
        ("cash", "Insufficient"),
        ("short", "Short selling"),
        ("missing_mark", "valuation"),
    ],
)
def test_gate_rejects_bad_inputs(setup, change, expected):
    quote, decision, controls, account, evidence, clock = setup
    if change == "stale":
        quote = quote.model_copy(update={"observed_at": utcnow() - timedelta(minutes=10)})
    elif change == "future":
        quote = quote.model_copy(update={"observed_at": utcnow() + timedelta(minutes=10)})
    elif change == "invented":
        evidence = set()
    elif change == "spread":
        quote = quote.model_copy(update={"bid": quote.ask * 0.9})
    elif change == "closed":
        clock = {"is_open": False}
    elif change == "daily":
        account["daily_pnl"] = -101
    elif change == "experiment":
        account["total_pnl"] = -251
    elif change == "positions":
        account["positions"] = [
            {"symbol": "OTHER", "underlying": "OTHER", "mark": 1, "remaining": 1, "multiplier": 1}
        ] * 3
    elif change == "liquidity":
        quote = quote.model_copy(update={"ask_size": 0})
    elif change == "cash":
        account["cash"] = 0
    elif change == "short":
        decision = decision.model_copy(update={"direction": "short"})
    elif change == "missing_mark":
        account["missing_marks"] = ["OTHER"]
    result = validate_trade(decision, quote, controls, account, evidence, clock)
    assert not result["accepted"]
    assert any(expected in reason for reason in result["reasons"])


def test_invalid_levels_rejected(setup):
    with pytest.raises(ValueError):
        Decision.model_validate({**setup[1].model_dump(), "stop": 10000})


def test_option_full_premium_sizing(setup):
    q, _, controls, account, _, clock = setup
    snapshot = demo_snapshot()
    q = Quote.model_validate(snapshot["quotes"][-1])
    d = demo_briefing(snapshot).decisions[-1]
    result = validate_trade(d, q, controls, account, {q.source_id}, clock)
    assert not result["accepted"]  # $25 cannot cover a $450 option contract.
    result = validate_trade(
        d, q, controls.model_copy(update={"risk_per_trade": 500}), account, {q.source_id}, clock
    )
    assert result["accepted"]
    assert result["qty"] == 1


def test_duplicate_events_immutable(store):
    assert store.append("decision", {"x": 1}, "same")
    assert not store.append("decision", {"x": 2}, "same")
    assert store.list()[0]["payload"] == {"x": 1}


def test_lease_excludes_second_worker_and_old_release(store):
    token = store.acquire("first")
    assert token
    assert not store.acquire("second")
    store.release("wrong-token")
    assert not store.acquire("second")
    store.release(token)
    assert store.acquire("second")


def test_analysis_lease_does_not_block_monitor(store):
    token = store.acquire("analysis", key="analysis_lease")
    assert token
    assert store.acquire("monitor")


def test_roundtrip_accounting_after_restart(store, monkeypatch):
    monkeypatch.setenv("MASTER_DEMO", "true")
    engine = Engine(store)
    engine.configure(Controls(mode="auto", state="enabled"))
    engine.scan()
    engine.tick()
    entries = store.list("entry")
    assert entries
    before = len(entries)
    engine.tick()
    assert len(store.list("entry")) == before
    book = entries[0]["payload"]["book"]
    snapshot = demo_snapshot()
    target = entries[0]["payload"]["target"]
    symbol = entries[0]["payload"]["symbol"]
    for q in snapshot["quotes"]:
        if q["symbol"] == symbol:
            q["bid"] = target + 1
            q["ask"] = target + 2
    engine.manage(snapshot)
    assert any(
        e["payload"]["position_id"] == entries[0]["payload"]["position_id"]
        for e in store.list("exit")
    )
    restarted = Engine(store)
    result = restarted.account(book, snapshot)
    assert result["realized_pnl"] > 0
    assert result["equity"] > 10000


def test_pausing_does_not_prevent_exits(store, monkeypatch):
    monkeypatch.setenv("MASTER_DEMO", "true")
    engine = Engine(store)
    engine.configure(Controls(mode="auto", state="enabled"))
    engine.scan()
    engine.tick()
    assert store.list("entry")
    engine.configure(Controls(mode="auto", state="manage_only"))
    snapshot = demo_snapshot()
    for q in snapshot["quotes"]:
        q["bid"] *= 0.8
        q["ask"] *= 0.8
    engine.manage(snapshot)
    assert store.list("exit")


def test_demo_never_calls_ai_or_broker(store, monkeypatch):
    monkeypatch.setenv("MASTER_DEMO", "true")
    monkeypatch.setenv("EXECUTION_BACKEND", "alpaca_paper")
    engine = Engine(store)
    engine.broker.cycle = lambda *args: pytest.fail("Demo must not invoke broker")
    engine.analyst.analyze = lambda *args: pytest.fail("Demo must not invoke AI")
    engine.scan()
    engine.tick()
    assert not store.list("broker_intent")


def test_authenticated_controls_and_journal(store, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-secret")
    app = create_app(store, start_worker=False)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/").status_code == 200
        assert client.get("/api/dashboard").status_code == 401
        headers = {"Authorization": "Bearer test-secret"}
        response = client.get("/api/dashboard", headers=headers)
        assert response.status_code == 200
        assert response.json()["live_enabled"] is False
        assert (
            client.put(
                "/api/controls",
                headers=headers,
                json=Controls(mode="auto", state="enabled").model_dump(),
            ).status_code
            == 200
        )
        assert client.post("/api/pause", headers=headers).json()["status"] == "manage_only"
        assert client.get("/api/export", headers=headers).status_code == 200
        assert "test-secret" not in client.get("/api/export", headers=headers).text


def test_auth_fails_closed_without_secret(store, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    with TestClient(create_app(store, start_worker=False)) as client:
        assert client.get("/api/dashboard").status_code == 503


def test_broker_intent_written_before_network_and_never_retried(store):
    broker = PaperBroker(store)
    broker.lease_token = store.acquire("test")
    calls = []

    def request(method, path, payload):
        assert store.list("broker_intent")
        calls.append(method)
        raise RuntimeError("simulated crash after submit")

    broker.request = request
    with pytest.raises(RuntimeError):
        broker.submit("mt-e-1", {"symbol": "AAPL"}, {"id": "1"})
    broker.submit("mt-e-1", {"symbol": "AAPL"}, {"id": "1"})
    assert calls == ["POST"]


def test_history_is_not_truncated_at_5000(store):
    for i in range(5010):
        store.append("fixture", {"i": i}, str(i))
    assert len(store.list()) == 5010


def test_scan_failure_has_no_demo_fallback(store, monkeypatch):
    monkeypatch.setenv("MASTER_DEMO", "false")
    engine = Engine(store)
    engine.research.snapshot = lambda: {
        "id": "test",
        "quotes": [],
        "news": [],
        "clock": None,
        "errors": [],
        "demo": False,
    }
    engine.scan()
    assert not store.list("decision")
    assert store.get("scan_status")["state"] == "error"


@pytest.mark.parametrize("index", [0, 3, 5])
def test_paper_broker_submits_supported_asset_orders(store, index):
    snapshot = demo_snapshot()
    snapshot["demo"] = False
    quote = snapshot["quotes"][index]
    decision = demo_briefing(snapshot).decisions[index].model_copy(update={"action": "trade"})
    record = {
        "id": str(index),
        "demo": False,
        "role": "champion",
        "plan": decision.model_dump(mode="json"),
        "quote": quote,
        "snapshot_id": snapshot["id"],
        "expires_at": (utcnow() + timedelta(hours=1)).isoformat(),
    }
    store.append("snapshot", snapshot, "test-snapshot")
    store.set("broker_initial_equity", 10000)
    broker = PaperBroker(store)
    broker.lease_token = store.acquire("test")
    sent = []

    def request(method, path, payload):
        sent.append(payload)
        return {"id": "broker-id", "status": "new", **payload}

    broker.request = request
    state = {
        "account": {"equity": "10000", "cash": "10000", "options_trading_level": 2},
        "positions": [],
        "orders": [],
        "unresolved": [],
        "daily_pnl": 0,
    }
    controls = Controls(risk_per_trade=500)
    broker.enter(record, snapshot, state, controls)
    assert len(sent) == 1
    assert sent[0]["symbol"] == quote["symbol"]
    assert sent[0]["client_order_id"].startswith("mt-e-")
    if quote["asset"] == "stock":
        assert sent[0]["order_class"] == "bracket"
    elif quote["asset"] == "option":
        assert sent[0]["position_intent"] == "buy_to_open"
    else:
        assert sent[0]["time_in_force"] == "gtc"
    broker.enter(record, snapshot, state, controls)
    assert len(sent) == 1


def test_unresolved_order_blocks_new_entry(store):
    snapshot = demo_snapshot()
    record = {
        "id": "new",
        "demo": False,
        "role": "champion",
        "plan": demo_briefing(snapshot).decisions[0].model_dump(mode="json"),
        "expires_at": (utcnow() + timedelta(hours=1)).isoformat(),
    }
    broker = PaperBroker(store)
    broker.request = lambda *args: pytest.fail("Unresolved order must block new submission")
    broker.enter(record, snapshot, {"unresolved": ["mt-e-unknown"]}, Controls())
    assert not store.list("broker_intent")


def test_paper_endpoint_cannot_be_redirected(store, monkeypatch):
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "false")
    assert PaperBroker(store).paper == "https://paper-api.alpaca.markets"
