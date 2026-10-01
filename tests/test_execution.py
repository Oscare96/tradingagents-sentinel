"""Tests for the deterministic paper-execution layer.

Covers: risk invariants, position sizing, bracket math, kill switch,
idempotent order submission, broker adapter behavior (mocked HTTP),
ledger round-trips, and full trading-cycle scenarios with a fake broker.
"""

import json
from datetime import datetime
from unittest.mock import patch

import pytest

from tradingagents.execution import config as cfg
from tradingagents.execution.broker import (
    AlpacaPaperBroker, BrokerAuthError, BrokerError,
)
from tradingagents.execution.ledger import Ledger, StateCorruptError
from tradingagents.execution.risk import (
    Intent, Position, RiskState, bracket_prices, daily_pnl_pct, find_orphans,
    gross_exposure_pct, kill_switch_tripped, size_position, validate_intent,
)
from tradingagents.execution import strategy as strat
from tradingagents.execution import trader as trader_mod
from tradingagents.scanner.market_calendar import ET


# ---------------------------------------------------------------- helpers
def make_intent(**kw):
    base = dict(symbol="AAA", side="buy", qty=10, reference_price=100.0,
                quote_price=100.0, strategy_version=cfg.STRATEGY_VERSION,
                client_order_id="sentinel-2026-10-01-AAA-buy-referencev1-1")
    base.update(kw)
    return Intent(**base)


def make_state(**kw):
    base = dict(equity=100_000.0, buying_power=100_000.0,
                day_start_equity=100_000.0, positions=(),
                open_order_symbols=frozenset(), open_client_order_ids=frozenset(),
                new_positions_today=0, kill_engaged=False, market_open=True)
    base.update(kw)
    return RiskState(**base)


def v(intent, state, **kw):
    k = dict(expected_strategy_version=cfg.STRATEGY_VERSION,
             max_position_pct=cfg.MAX_POSITION_PCT,
             max_concurrent_positions=cfg.MAX_CONCURRENT_POSITIONS,
             max_gross_exposure_pct=cfg.MAX_GROSS_EXPOSURE_PCT,
             max_new_positions_per_day=cfg.MAX_NEW_POSITIONS_PER_DAY,
             max_quote_drift_pct=cfg.MAX_QUOTE_DRIFT_PCT)
    k.update(kw)
    return validate_intent(intent, state, **k)


# ---------------------------------------------------------------- sizing
def test_size_position_floors_to_whole_shares():
    assert size_position(100_000, 100.0, 0.02) == 20
    assert size_position(100_000, 30.0, 0.02) == 66  # floor(2000/30)


def test_size_position_too_small_returns_zero():
    assert size_position(100_000, 5000.0, 0.02) == 0
    assert size_position(0, 100.0, 0.02) == 0
    assert size_position(100_000, 0, 0.02) == 0


def test_bracket_prices_math():
    stop, target = bracket_prices(100.0, 0.04, 0.08)
    assert stop == 96.0
    assert target == 108.0


def test_kill_switch_boundary():
    assert kill_switch_tripped(98_000, 100_000, 0.02) is True   # exactly -2%
    assert kill_switch_tripped(98_001, 100_000, 0.02) is False  # -1.999%
    assert kill_switch_tripped(101_000, 100_000, 0.02) is False


def test_daily_pnl_pct():
    assert daily_pnl_pct(98_000, 100_000) == pytest.approx(-0.02)
    assert daily_pnl_pct(100_000, 0) == 0.0


def test_gross_exposure():
    pos = (Position("A", 10, 5_000.0), Position("B", 5, 3_000.0))
    assert gross_exposure_pct(pos, 100_000) == pytest.approx(0.08)


def test_find_orphans():
    assert find_orphans({"A", "B"}, {"A"}) == {"B"}
    assert find_orphans({"A"}, {"A"}) == set()


# ---------------------------------------------------------------- validate_intent
def test_valid_intent_approved():
    assert v(make_intent(), make_state()) == []


def test_kill_engaged_rejects_everything():
    reasons = v(make_intent(), make_state(kill_engaged=True))
    assert reasons == ["kill switch engaged"]


def test_market_closed_rejects():
    assert any("closed" in r for r in v(make_intent(), make_state(market_open=False)))


def test_already_held_rejects():
    st = make_state(positions=(Position("AAA", 10, 1_000.0),))
    assert any("already hold" in r for r in v(make_intent(), st))


def test_open_order_for_symbol_rejects():
    st = make_state(open_order_symbols=frozenset({"AAA"}))
    assert any("open order" in r for r in v(make_intent(), st))


def test_duplicate_client_order_id_rejects():
    st = make_state(open_client_order_ids=frozenset(
        {"sentinel-2026-10-01-AAA-buy-referencev1-1"}))
    assert any("already submitted" in r for r in v(make_intent(), st))


def test_daily_new_position_limit_rejects():
    st = make_state(new_positions_today=cfg.MAX_NEW_POSITIONS_PER_DAY)
    assert any("daily new-position" in r for r in v(make_intent(), st))


def test_position_too_big_rejects():
    big = make_intent(qty=60, reference_price=100.0)  # $6k > 5% of 100k
    assert any("exceeds" in r for r in v(big, make_state()))


def test_max_concurrent_positions_rejects():
    pos = tuple(Position(f"S{i}", 10, 1_000.0)
                for i in range(cfg.MAX_CONCURRENT_POSITIONS))
    assert any("max concurrent" in r for r in v(make_intent(), make_state(positions=pos)))


def test_gross_exposure_cap_rejects():
    pos = (Position("X", 100, 24_500.0),)  # 24.5% + 1% new = 25.5% > 25% cap
    assert any("exposure" in r for r in v(make_intent(), make_state(positions=pos)))


def test_insufficient_buying_power_rejects():
    st = make_state(buying_power=500.0)
    assert any("buying power" in r for r in v(make_intent(), st))


def test_quote_drift_rejects():
    drifted = make_intent(reference_price=100.0, quote_price=103.0)  # +3%
    assert any("drifted" in r for r in v(drifted, make_state()))


def test_quote_drift_within_cap_passes():
    ok = make_intent(reference_price=100.0, quote_price=101.0)  # +1%
    assert v(ok, make_state()) == []


def test_wrong_strategy_version_rejects():
    bad = make_intent(strategy_version="something-else-v9")
    assert any("strategy version" in r for r in v(bad, make_state()))


def test_zero_qty_rejects():
    assert any("quantity" in r for r in v(make_intent(qty=0), make_state()))


def test_short_side_rejects():
    assert any("long-only" in r for r in v(make_intent(side="sell"), make_state()))


# ---------------------------------------------------------------- strategy
def _strat_cand(ticker, score, last):
    return {"ticker": ticker, "score": score, "facts": {"last": last}}


def test_strategy_picks_best_candidate_above_bar():
    cands = [_strat_cand("BBB", 5.0, 50.0), _strat_cand("AAA", 7.5, 100.0),
             _strat_cand("CCC", 9.0, 20.0)]
    intents = strat.generate_intents(cands, 100_000, set(), set(), "2026-10-01")
    assert len(intents) == 1
    assert intents[0].symbol == "CCC"  # highest score first
    assert intents[0].qty == 100  # 2% of 100k / 20


def test_strategy_skips_below_bar_held_and_ordered():
    cands = [_strat_cand("LOW", 4.0, 10.0), _strat_cand("HELD", 8.0, 10.0),
             _strat_cand("ORD", 8.0, 10.0)]
    intents = strat.generate_intents(cands, 100_000, {"HELD"}, {"ORD"}, "2026-10-01")
    assert intents == []


def test_strategy_respects_max_new_per_day():
    cands = [_strat_cand("A", 8.0, 10.0), _strat_cand("B", 8.0, 10.0)]
    intents = strat.generate_intents(cands, 100_000, set(), set(),
                                     "2026-10-01", max_new=1)
    assert len(intents) == 1


def test_client_order_id_deterministic():
    a = strat.build_client_order_id("2026-10-01", "AAA", "buy",
                                    cfg.STRATEGY_VERSION, 1)
    b = strat.build_client_order_id("2026-10-01", "AAA", "buy",
                                    cfg.STRATEGY_VERSION, 1)
    c = strat.build_client_order_id("2026-10-01", "AAA", "buy",
                                    cfg.STRATEGY_VERSION, 2)
    assert a == b != c
    assert a.startswith("sentinel-2026-10-01-AAA-buy-")


# ---------------------------------------------------------------- broker (mocked HTTP)
class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _broker():
    return AlpacaPaperBroker(key_id="K", secret_key="S")


# ---------------------------------------------------------------- order-path purity
def test_no_llm_imports_in_execution_package():
    """No LLM library may appear in the order path -- deterministic only."""
    import ast
    from pathlib import Path
    LLM_LIBS = {"langchain", "langgraph", "openai", "anthropic", "google"}
    pkg = Path(__file__).resolve().parents[1] / "tradingagents" / "execution"
    offenders = []
    for path in pkg.rglob("*.py"):
        names = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module.split(".")[0])
        bad = names & LLM_LIBS
        if bad:
            offenders.append(f"{path.name}: {sorted(bad)}")
    assert offenders == []


def test_broker_points_at_paper_only():
    b = _broker()
    assert b.PAPER_BASE_URL == "https://paper-api.alpaca.markets"
    with patch("requests.request") as req:
        req.return_value = _Resp({"equity": "100000"})
        b.get_account()
        url = req.call_args[0][1]
        assert url.startswith("https://paper-api.alpaca.markets/v2/account")
        assert "api.alpaca.markets" not in url.replace("paper-api.alpaca.markets", "")


def test_broker_auth_headers():
    b = _broker()
    with patch("requests.request") as req:
        req.return_value = _Resp({})
        b.get_positions()
        headers = req.call_args[1]["headers"]
        assert headers["APCA-API-KEY-ID"] == "K"
        assert headers["APCA-API-SECRET-KEY"] == "S"


def test_broker_401_raises_auth_error():
    b = _broker()
    with patch("requests.request") as req:
        req.return_value = _Resp({"message": "unauthorized"}, status=401)
        with pytest.raises(BrokerAuthError):
            b.get_account()


def test_broker_missing_credentials_fails_closed():
    with patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop(cfg.APCA_KEY_ENV, None)
        os.environ.pop(cfg.APCA_SECRET_ENV, None)
        with pytest.raises(BrokerAuthError):
            AlpacaPaperBroker()


def test_bracket_payload_shape():
    b = _broker()
    with patch("requests.request") as req:
        req.return_value = _Resp({"id": "order-1"})
        b.submit_bracket_order("AAA", 10, "cid-1", 96.0, 108.0)
        payload = req.call_args[1]["json"]
        assert payload["order_class"] == "bracket"
        assert payload["type"] == "market"
        assert payload["time_in_force"] == "day"
        assert payload["client_order_id"] == "cid-1"
        assert payload["stop_loss"] == {"stop_price": 96.0}
        assert payload["take_profit"] == {"limit_price": 108.0}


def test_broker_error_includes_status():
    b = _broker()
    with patch("requests.request") as req:
        req.return_value = _Resp({"message": "bad"}, status=422)
        with pytest.raises(BrokerError, match="422"):
            b.submit_bracket_order("AAA", 10, "cid-1", 96.0, 108.0)


# ---------------------------------------------------------------- ledger
def test_ledger_record_and_read(tmp_path):
    led = Ledger(root=tmp_path)
    led.record("2026-10-01", "order_submitted", symbol="AAA", qty=10)
    rows = led.read_day("2026-10-01")
    assert len(rows) == 1
    assert rows[0]["event"] == "order_submitted"
    assert rows[0]["symbol"] == "AAA"
    assert "ts" in rows[0]


def test_ledger_state_round_trip(tmp_path):
    led = Ledger(root=tmp_path)
    led.save_state({"date": "2026-10-01", "new_positions_today": 2})
    assert led.load_state()["new_positions_today"] == 2


def test_kill_flag(tmp_path):
    led = Ledger(root=tmp_path)
    assert not led.kill_engaged()
    led.engage_kill("2026-10-01", "test trip")
    assert led.kill_engaged()
    rows = led.read_day("2026-10-01")
    assert any(r["event"] == "kill_switch_engaged" for r in rows)


# ---------------------------------------------------------------- trader with fake broker
class FakeBroker:
    """In-memory stand-in for AlpacaPaperBroker."""

    def __init__(self, equity=100_000.0, positions=None, orders=None,
                 quotes=None, fail_on=None, closed_orders=None,
                 base_value=None):
        self.equity = equity
        self.positions = positions or {}   # symbol -> dict(qty, market_value)
        self.orders = orders or []         # open orders
        self.closed_orders = closed_orders or []  # order history (closed)
        self.base_value = base_value       # portfolio history base_value
        self.quotes = quotes or {}
        self.fail_on = fail_on or set()
        self.submitted = []
        self.cancelled_all = 0
        self.closed_all = 0

    def _maybe_fail(self, name):
        if name in self.fail_on:
            raise BrokerError(f"fake failure: {name}")

    def connect(self):
        self._maybe_fail("connect")
        return {"equity": str(self.equity), "buying_power": str(self.equity),
                "account_number": "PA123"}

    def get_positions(self):
        self._maybe_fail("positions")
        return [{"symbol": s, "qty": str(v["qty"]),
                 "market_value": str(v["market_value"])}
                for s, v in self.positions.items()]

    def get_open_orders(self):
        return self.orders

    def get_orders(self, status="all", limit=500):
        self._maybe_fail("orders")
        if status == "closed":
            return self.closed_orders
        return self.orders + self.closed_orders

    def get_portfolio_history(self, period="1D"):
        self._maybe_fail("history")
        return {"base_value": self.base_value if self.base_value is not None
                else self.equity,
                "equity": [], "timestamp": []}

    def get_latest_ask(self, symbol):
        if symbol not in self.quotes:
            raise BrokerError(f"no quote for {symbol}")
        return self.quotes[symbol]

    def submit_bracket_order(self, symbol, qty, client_order_id, stop_price,
                             take_profit_price, side="buy"):
        self._maybe_fail("submit")
        if any(o.get("client_order_id") == client_order_id for o in self.orders):
            raise BrokerError("duplicate client_order_id")
        order = {"id": f"bo-{len(self.submitted)}", "symbol": symbol,
                 "qty": str(qty), "client_order_id": client_order_id,
                 "stop_price": stop_price, "take_profit_price": take_profit_price}
        self.orders.append(order)
        self.submitted.append(order)
        return order

    def cancel_all_orders(self):
        self.cancelled_all += 1  # attempts, even if the call then fails
        self._maybe_fail("cancel")
        self.orders = []

    def close_all_positions(self):
        self.closed_all += 1  # attempts, even if the call then fails
        self._maybe_fail("close")
        self.positions = {}


def _noon(day="2026-10-01"):
    return ET.localize(datetime(2026, 10, 1, 12, 0))


def _cand(ticker, score, last, price_asof="2026-10-01T12:00:00-04:00"):
    return {"ticker": ticker, "score": score,
            "facts": {"last": last, "price_asof": price_asof}}


def _scan(trade_date="2026-10-01", cands=None,
          scanned_at="2026-10-01T12:00:00-04:00"):
    return {"trade_date": trade_date, "scanned_at": scanned_at,
            "watchlist": cands if cands is not None
            else [_cand("AAA", 8.0, 100.0)]}


def test_cycle_submits_one_bracket(tmp_path):
    led = Ledger(root=tmp_path)
    broker = FakeBroker(quotes={"AAA": 100.5})
    out = trader_mod.run_trading_cycle(_scan(), broker, led, now=_noon())
    assert len(broker.submitted) == 1
    order = broker.submitted[0]
    assert order["symbol"] == "AAA" and order["qty"] == "20"
    # Legs anchor to the fresh pre-submit quote (100.5), not the scan price.
    assert order["stop_price"] == 96.48 and order["take_profit_price"] == 108.54
    assert any(a["action"] == "order_submitted" for a in out["actions"])
    state = led.load_state()
    assert state["new_positions_today"] == 1
    assert state["day_start_equity"] == 100_000.0


def test_cycle_idempotent_on_restart(tmp_path):
    """Second cycle sees the open order and does not resubmit."""
    led = Ledger(root=tmp_path)
    broker = FakeBroker(quotes={"AAA": 100.5})
    trader_mod.run_trading_cycle(_scan(), broker, led, now=_noon())
    out = trader_mod.run_trading_cycle(_scan(), broker, led, now=_noon())
    assert len(broker.submitted) == 1
    assert not any(a["action"] == "order_submitted" for a in out["actions"])


def test_kill_switch_trips_on_daily_loss(tmp_path):
    led = Ledger(root=tmp_path)
    broker = FakeBroker(equity=100_000.0, quotes={"AAA": 100.0},
                        positions={"AAA": {"qty": 20, "market_value": 2_000.0}})
    trader_mod.run_trading_cycle(_scan(cands=[]), broker, led, now=_noon())
    broker.equity = 97_000.0  # -3% vs day start
    out = trader_mod.run_trading_cycle(_scan(cands=[]), broker, led, now=_noon())
    assert led.kill_engaged()
    assert broker.cancelled_all == 1 and broker.closed_all == 1
    # Next cycle keeps retrying emergency cleanup until the broker confirms
    # flat; entries stay blocked until a human clears the flag.
    broker2 = FakeBroker(quotes={"AAA": 100.0})
    out2 = trader_mod.run_trading_cycle(_scan(), broker2, led, now=_noon())
    assert any(a["action"] == "kill_switch_flat_confirmed"
               for a in out2["actions"])
    assert not any(a["action"] == "order_submitted" for a in out2["actions"])
    assert broker2.submitted == []


def test_restart_with_lost_state_does_not_resubmit(tmp_path, monkeypatch):
    """State file wiped (crash) but the broker still shows the open order:
    the trader must not double-submit."""
    monkeypatch.setattr(cfg, "MAX_NEW_POSITIONS_PER_DAY", 2)
    led = Ledger(root=tmp_path)
    broker = FakeBroker(quotes={"AAA": 100.0})
    trader_mod.run_trading_cycle(_scan(), broker, led, now=_noon())
    assert len(broker.submitted) == 1
    (tmp_path / "state.json").unlink()  # simulate crash losing local state
    out = trader_mod.run_trading_cycle(_scan(), broker, led, now=_noon())
    assert len(broker.submitted) == 1
    assert not any(a["action"] == "order_submitted" for a in out["actions"])


def test_eod_flatten(tmp_path):
    led = Ledger(root=tmp_path)
    broker = FakeBroker(quotes={"AAA": 100.0},
                        positions={"AAA": {"qty": 20, "market_value": 2_000.0}})
    late = ET.localize(datetime(2026, 10, 1, 15, 50))
    out = trader_mod.run_trading_cycle(_scan(cands=[]), broker, led, now=late)
    assert broker.closed_all == 1
    assert any(a["action"] == "eod_flatten" for a in out["actions"])


def test_orphan_blocks_entries(tmp_path):
    led = Ledger(root=tmp_path)
    broker = FakeBroker(quotes={"AAA": 100.0},
                        positions={"ZZZ": {"qty": 5, "market_value": 500.0}})
    out = trader_mod.run_trading_cycle(_scan(), broker, led, now=_noon())
    assert broker.submitted == []
    assert any(a["action"] == "orphan_positions" for a in out["actions"])


def test_stale_scan_skips_entries(tmp_path):
    led = Ledger(root=tmp_path)
    broker = FakeBroker(quotes={"AAA": 100.0})
    out = trader_mod.run_trading_cycle(_scan(trade_date="2026-09-30"),
                                       broker, led, now=_noon())
    assert broker.submitted == []
    assert any(a["action"] == "entries_skipped" for a in out["actions"])


def test_broker_failure_fails_closed(tmp_path):
    led = Ledger(root=tmp_path)
    broker = FakeBroker(fail_on={"connect"})
    out = trader_mod.run_trading_cycle(_scan(), broker, led, now=_noon())
    assert broker.submitted == []
    assert any(a["action"] == "cycle_aborted" for a in out["actions"])


def test_submit_failure_does_not_retry_blindly(tmp_path):
    led = Ledger(root=tmp_path)
    broker = FakeBroker(quotes={"AAA": 100.0}, fail_on={"submit"})
    out = trader_mod.run_trading_cycle(_scan(), broker, led, now=_noon())
    assert broker.submitted == []
    assert led.load_state()["consecutive_rejections"] == 1
    assert not led.kill_engaged()  # single failure does not trip the kill
    assert any(a["action"] == "order_failed" for a in out["actions"])


# ------------------------------------------- regression: Oct 1 safety review
def test_no_entry_after_cutoff_with_empty_account(tmp_path):
    """Review finding 1: the old flatten branch skipped empty accounts,
    leaving the entry path open at 15:50. Past the cutoff, entries are
    blocked even with nothing to flatten."""
    led = Ledger(root=tmp_path)
    broker = FakeBroker(quotes={"AAA": 100.0})
    late = ET.localize(datetime(2026, 10, 1, 15, 50))
    out = trader_mod.run_trading_cycle(_scan(), broker, led, now=late)
    assert broker.submitted == []
    assert any(a["action"] == "past_eod_cutoff" for a in out["actions"])


def test_early_close_moves_cutoff(tmp_path, monkeypatch):
    """Review finding 1b: on a 13:00 ET early close the cutoff is 12:45,
    not 15:45."""
    led = Ledger(root=tmp_path)
    broker = FakeBroker(quotes={"AAA": 100.0})
    monkeypatch.setattr(trader_mod, "session_close",
                        lambda now: ET.localize(datetime(2026, 10, 1, 13, 0)))
    late = ET.localize(datetime(2026, 10, 1, 12, 50))
    out = trader_mod.run_trading_cycle(_scan(), broker, led, now=late)
    assert broker.submitted == []
    assert any(a["action"] == "past_eod_cutoff" for a in out["actions"])


def test_kill_switch_retries_failed_liquidation(tmp_path):
    """Review finding 2: a failed liquidation is retried every cycle until
    the broker confirms flat -- never abandoned after one attempt."""
    led = Ledger(root=tmp_path)
    broker = FakeBroker(equity=100_000.0, quotes={"AAA": 100.0},
                        positions={"AAA": {"qty": 20, "market_value": 2_000.0}},
                        fail_on={"close"})
    trader_mod.run_trading_cycle(_scan(cands=[]), broker, led, now=_noon())
    broker.equity = 97_000.0  # -3% -> trip, but the close fails
    trader_mod.run_trading_cycle(_scan(cands=[]), broker, led, now=_noon())
    assert led.kill_engaged()
    assert broker.closed_all == 1
    assert "AAA" in broker.positions  # still open: the close failed

    # Cycle 2: kill engaged -> cleanup retried, still failing.
    out2 = trader_mod.run_trading_cycle(_scan(), broker, led, now=_noon())
    assert broker.closed_all == 2
    assert any(a["action"] == "kill_switch_cleanup_failed"
               for a in out2["actions"])
    assert broker.submitted == []  # entries stay blocked

    # Cycle 3: broker recovers -> cleanup succeeds, flat confirmed.
    broker.fail_on = set()
    out3 = trader_mod.run_trading_cycle(_scan(), broker, led, now=_noon())
    assert broker.closed_all == 3
    assert any(a["action"] == "kill_switch_flat_confirmed"
               for a in out3["actions"])
    assert broker.submitted == []  # still blocked: human clears the flag


def test_corrupt_state_raises_instead_of_resetting(tmp_path):
    led = Ledger(root=tmp_path)
    (tmp_path / "state.json").write_text("{nope")
    with pytest.raises(StateCorruptError):
        led.load_state()


def test_corrupt_state_reconstructs_from_broker(tmp_path):
    """Review finding 3: unreadable state.json rebuilds limits from broker
    history instead of resetting them."""
    led = Ledger(root=tmp_path)
    (tmp_path / "state.json").write_text("{not valid json")
    broker = FakeBroker(
        quotes={"AAA": 100.0},
        closed_orders=[{"id": "o1", "symbol": "AAA", "side": "buy",
                        "status": "filled", "filled_qty": "20",
                        "filled_avg_price": "100.0",
                        "filled_at": "2026-10-01T14:00:00Z",
                        "client_order_id": "sentinel-20261001-AAA-buy-v1-1"}],
        base_value=99_000.0)
    out = trader_mod.run_trading_cycle(_scan(cands=[]), broker, led, now=_noon())
    assert any(a["action"] == "state_reconstructed" for a in out["actions"])
    state = led.load_state()
    assert state["new_positions_today"] == 1
    assert state["day_start_equity"] == 99_000.0


def test_corrupt_state_blocks_entries_when_history_unavailable(tmp_path):
    """Review finding 3b: if broker history is also unavailable, fail closed
    (entries blocked, management continues) instead of resetting limits."""
    led = Ledger(root=tmp_path)
    (tmp_path / "state.json").write_text("{not valid json")
    broker = FakeBroker(quotes={"AAA": 100.0}, fail_on={"orders", "history"})
    out = trader_mod.run_trading_cycle(_scan(), broker, led, now=_noon())
    assert broker.submitted == []
    assert any(a["action"] == "entries_blocked" for a in out["actions"])


def test_stale_scan_age_blocks_entries(tmp_path):
    """Review finding 4: a two-hour-old scan on the same trade_date must not
    trade."""
    led = Ledger(root=tmp_path)
    broker = FakeBroker(quotes={"AAA": 100.0})
    out = trader_mod.run_trading_cycle(
        _scan(scanned_at="2026-10-01T10:00:00-04:00"), broker, led, now=_noon())
    assert broker.submitted == []
    skipped = [a for a in out["actions"] if a["action"] == "entries_skipped"]
    assert skipped and any("120 min old" in r for r in skipped[0]["reasons"])


def test_stale_price_asof_rejects_intent(tmp_path):
    """Review finding 4b: candidate prices older than the cap are rejected."""
    led = Ledger(root=tmp_path)
    broker = FakeBroker(quotes={"AAA": 100.0})
    scan = _scan(cands=[_cand("AAA", 8.0, 100.0,
                              price_asof="2026-10-01T10:00:00-04:00")])
    out = trader_mod.run_trading_cycle(scan, broker, led, now=_noon())
    assert broker.submitted == []
    rejected = [a for a in out["actions"] if a["action"] == "intent_rejected"]
    assert rejected and any("price_asof" in r for r in rejected[0]["reasons"])


def test_three_consecutive_rejections_trip_kill(tmp_path):
    """Review finding 5a: the documented 3-rejection kill switch now trips."""
    led = Ledger(root=tmp_path)
    broker = FakeBroker(quotes={"AAA": 100.0}, fail_on={"submit"})
    for _ in range(3):
        trader_mod.run_trading_cycle(_scan(), broker, led, now=_noon())
    assert led.kill_engaged()
    assert broker.cancelled_all >= 1  # emergency cleanup ran on the trip
    assert led.load_state()["consecutive_rejections"] == 3


def test_fill_reconciliation_records_fills(tmp_path):
    """Review finding 5b: closed orders matching our submissions are
    recorded as fills."""
    led = Ledger(root=tmp_path)
    broker = FakeBroker(quotes={"AAA": 100.0})
    trader_mod.run_trading_cycle(_scan(), broker, led, now=_noon())
    cid = broker.submitted[0]["client_order_id"]
    broker.closed_orders = [{"id": "bo-0", "symbol": "AAA", "side": "buy",
                             "status": "filled", "filled_qty": "20",
                             "filled_avg_price": "100.4",
                             "filled_at": "2026-10-01T16:05:00Z",
                             "client_order_id": cid}]
    out = trader_mod.run_trading_cycle(_scan(cands=[]), broker, led, now=_noon())
    fills = led.fills("2026-10-01")
    assert len(fills) == 1
    assert fills[0]["price"] == 100.4 and fills[0]["qty"] == 20
    assert any(a["action"] == "fill" for a in out["actions"])


def test_partial_fill_is_flagged(tmp_path):
    led = Ledger(root=tmp_path)
    broker = FakeBroker(quotes={"AAA": 100.0})
    trader_mod.run_trading_cycle(_scan(), broker, led, now=_noon())
    cid = broker.submitted[0]["client_order_id"]
    broker.closed_orders = [{"id": "bo-0", "symbol": "AAA", "side": "buy",
                             "status": "filled", "filled_qty": "12",
                             "filled_avg_price": "100.4",
                             "filled_at": "2026-10-01T16:05:00Z",
                             "client_order_id": cid}]
    out = trader_mod.run_trading_cycle(_scan(cands=[]), broker, led, now=_noon())
    partials = [a for a in out["actions"] if a["action"] == "partial_fill"]
    assert partials and partials[0]["qty"] == 12
    assert partials[0]["ordered_qty"] == 20


def test_realized_pnl_fifo_from_fills(tmp_path):
    """Review finding 5c: realized P&L is computed from recorded fills."""
    led = Ledger(root=tmp_path)
    led.record("2026-10-01", "fill", symbol="AAA", side="buy", qty=20, price=100.0)
    led.record("2026-10-01", "fill", symbol="AAA", side="buy", qty=10, price=102.0)
    led.record("2026-10-01", "fill", symbol="AAA", side="sell", qty=25, price=108.0)
    pnl = led.realized_pnl("2026-10-01")
    # 20*(108-100) + 5*(108-102) = 160 + 30
    assert pnl == {"AAA": 190.0}


# ------------------------------------------- scheduler wiring (finding 6)
def _svc():
    from tradingagents.dashboard.scanner_service import ScannerService
    return ScannerService()


def test_maybe_trade_disabled_by_default(monkeypatch):
    monkeypatch.setattr("tradingagents.execution.config.EXEC_TRADING_ENABLED",
                        False)
    called = []
    monkeypatch.setattr("tradingagents.execution.trader.run_trading_cycle",
                        lambda *a, **k: called.append(1))
    _svc()._maybe_trade({"trade_date": "2026-10-01"})
    assert called == []


def test_maybe_trade_needs_keys(monkeypatch):
    from tradingagents.execution import config as exc_cfg
    monkeypatch.setattr(exc_cfg, "EXEC_TRADING_ENABLED", True)
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    called = []
    monkeypatch.setattr("tradingagents.execution.trader.run_trading_cycle",
                        lambda *a, **k: called.append(1))
    _svc()._maybe_trade({"trade_date": "2026-10-01"})
    assert called == []


def test_maybe_trade_calls_cycle_when_enabled_with_keys(monkeypatch):
    from tradingagents.execution import config as exc_cfg
    monkeypatch.setattr(exc_cfg, "EXEC_TRADING_ENABLED", True)
    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s")
    called = []
    monkeypatch.setattr(
        "tradingagents.execution.trader.run_trading_cycle",
        lambda scan, broker, ledger: called.append(scan) or {"actions": []})
    _svc()._maybe_trade({"trade_date": "2026-10-01"})
    assert len(called) == 1
