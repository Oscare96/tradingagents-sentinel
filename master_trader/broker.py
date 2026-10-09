"""Alpaca PAPER-only execution with durable intents and no blind POST retries.

Stock entries use broker brackets. Crypto and bought-option exits are software
monitored in paper mode; this adapter deliberately cannot reach a live endpoint.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import httpx

from .engine import parse_time, validate_trade
from .models import Decision, Quote, timestamp, utcnow
from .providers import AlpacaResearch, ProviderError
from .store import canonical

TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}


class PaperBroker(AlpacaResearch):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.lease_token = None

    def request(self, method, path, payload=None):
        if method != "GET" and not self.store.lease_valid(self.lease_token):
            raise ProviderError("Execution lease unavailable for broker mutation")
        try:
            response = httpx.request(
                method, self.paper + path, headers=self.headers, json=payload, timeout=15
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                "Paper broker request outcome unknown; reconciliation required"
            ) from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ProviderError(f"Paper broker {method} {path}: HTTP {response.status_code}")
        return response.json() if response.content else {}

    def submit(self, client_id, payload, decision, intent_type="entry"):
        if not self.store.lease_valid(self.lease_token):
            raise ProviderError("Execution lease missing or expiring; submission blocked")
        # Commit intent before any network mutation. Crash after POST is reconciled by client ID.
        intent = {
            "client_order_id": client_id,
            "payload": payload,
            "decision": decision,
            "intent_type": intent_type,
            "at": timestamp(),
        }
        if not self.store.append("broker_intent", intent, "broker-intent:" + client_id):
            return
        try:
            result = self.request("POST", "/v2/orders", payload)
            if not result:
                raise ProviderError("Paper broker returned no order")
            self.store.append("broker_order", result, "broker-response:" + client_id)
        except ProviderError as exc:
            self.store.append(
                "broker_issue",
                {"client_order_id": client_id, "error": str(exc)},
                "broker-issue:" + client_id,
            )
            # Never retry here. Including HTTP rejection, retain record for reconciliation.

    def reconcile(self):
        account = self.get("/v2/account", paper=True)
        positions = self.get("/v2/positions", paper=True)
        orders = self.get(
            "/v2/orders", {"status": "all", "limit": 500, "nested": "true"}, paper=True
        )

        def flatten(items):
            result = []
            for item in items:
                result.append(item)
                result.extend(flatten(item.get("legs") or []))
            return result

        all_orders = flatten(orders)
        intents = [e["payload"] for e in self.store.list("broker_intent")]
        known = {o.get("client_order_id"): o for o in all_orders}
        unresolved = []
        for intent in intents:
            cid = intent["client_order_id"]
            saved = self.store.get("broker-status:" + cid)
            order = known.get(cid)
            if not order and saved and saved.get("status") in TERMINAL:
                continue
            if not order:
                order = self.request("GET", "/v2/orders:by_client_order_id?client_order_id=" + cid)
            if not order:
                # Definitive GET 404 means no found order, but do not assume a safe resubmit.
                unresolved.append(cid)
                continue
            self.store.set("broker-status:" + cid, order)
            digest = hashlib.sha256(canonical(order).encode()).hexdigest()
            self.store.append("broker_order", order, "broker-status:" + cid + ":" + digest)
            known[cid] = order
            for leg in order.get("legs") or []:
                known[leg.get("client_order_id", leg["id"])] = leg
        baseline = self.store.get("broker_initial_equity")
        if baseline is None:
            baseline = float(account["equity"])
            self.store.set("broker_initial_equity", baseline)
        day_key = "broker-day:" + utcnow().date().isoformat()
        day_start = self.store.get(day_key)
        if day_start is None:
            prior = [
                e["payload"]["equity"]
                for e in self.store.list("broker_equity")
                if e["payload"]["at"][:10] < utcnow().date().isoformat()
            ]
            day_start = prior[-1] if prior else baseline
            self.store.set(day_key, day_start)
        state = {
            "account": {
                k: account.get(k)
                for k in (
                    "equity",
                    "cash",
                    "buying_power",
                    "trading_blocked",
                    "account_blocked",
                    "options_trading_level",
                )
            },
            "positions": positions,
            "orders": list(known.values()),
            "unresolved": unresolved,
            "initial_equity": baseline,
            "daily_pnl": float(account["equity"]) - day_start,
            "total_pnl": float(account["equity"]) - baseline,
            "at": timestamp(),
            "endpoint": self.paper,
            "protection": "Stocks: broker bracket. Crypto/options: software exits; paper only.",
        }
        self.store.set("broker_state", state)
        self.store.append(
            "broker_equity",
            {
                "at": timestamp(),
                "equity": float(account["equity"]),
                "total_pnl": state["total_pnl"],
            },
            "broker-equity:" + timestamp(),
        )
        return state

    def manage(self, state, snapshot):
        quotes = {q["symbol"]: q for q in snapshot["quotes"]}
        intents = [
            e["payload"]
            for e in self.store.list("broker_intent")
            if e["payload"]["intent_type"] == "entry"
        ]
        orders = state["orders"]
        for position in state["positions"]:
            symbol = position["symbol"]
            # Latest owned entry supplies its immutable exit policy.
            owned = [
                i
                for i in intents
                if i["decision"]["plan"]["symbol"].replace("/", "") == symbol.replace("/", "")
            ]
            if not owned:
                continue  # Never close someone else's positions.
            intent = owned[-1]
            plan = intent["decision"]["plan"]
            quote = quotes.get(plan["symbol"])
            if (
                not quote
                or not -5 <= (utcnow() - parse_time(quote["observed_at"])).total_seconds() <= 60
            ):
                continue
            if quote["asset"] != "crypto" and not (snapshot["clock"] or {}).get("is_open"):
                continue
            is_long = float(position["qty"]) > 0
            price = quote["bid"] if is_long else quote["ask"]
            time_exit = (
                parse_time(intent["at"]) + timedelta(hours=plan["holding_hours"]) <= utcnow()
            )
            requested = self.store.get("close_requested")
            owner_exit = bool(requested and intent["at"] <= requested)
            expiry_exit = (
                quote.get("expiration")
                and quote["expiration"] <= (utcnow() + timedelta(days=1)).date().isoformat()
            )
            close = (snapshot["clock"] or {}).get("next_close")
            session_exit = (
                plan["style"] == "day"
                and quote["asset"] != "crypto"
                and close
                and (parse_time(close) - utcnow()).total_seconds() <= 300
            )
            if plan["style"] == "day" and quote["asset"] == "crypto":
                session_exit = parse_time(intent["at"]).date() < utcnow().date()
            stop = price <= plan["stop"] if is_long else price >= plan["stop"]
            target = price >= plan["target"] if is_long else price <= plan["target"]
            if not any((time_exit, expiry_exit, session_exit, stop, target, owner_exit)):
                continue
            active = [o for o in orders if o["symbol"] == symbol and o["status"] not in TERMINAL]
            # Bracket legs already implement stop/target. Cancel only for time/expiry closure.
            if quote["asset"] == "stock" and not any(
                (time_exit, expiry_exit, session_exit, owner_exit)
            ):
                continue
            if any(str(o.get("client_order_id", "")).startswith("mt-x-") for o in active):
                continue
            if active:
                # Cancel first, wait for a later reconciliation before closing. Avoid sell overlap.
                for order in active:
                    self.request("DELETE", "/v2/orders/" + order["id"])
                continue
            # Unique close attempt per position and remaining quantity. Never blindly re-submit.
            remaining = abs(float(position["qty"]))
            digest = hashlib.sha256(
                f"{intent['client_order_id']}:{remaining}".encode()
            ).hexdigest()[:24]
            attempts = [
                e["payload"]
                for e in self.store.list("broker_intent")
                if e["payload"]["intent_type"] == "exit"
                and e["payload"]["decision"]["id"] == intent["decision"]["id"]
            ]
            if any(i["client_order_id"] in state["unresolved"] for i in attempts):
                continue
            client_id = "mt-x-" + digest[:20] + "-" + str(len(attempts))
            exit_price = round(price * (0.995 if is_long else 1.005), 2 if price >= 1 else 4)
            payload = {
                "symbol": plan["symbol"],
                "qty": str(remaining),
                "side": "sell" if is_long else "buy",
                "type": "limit",
                "limit_price": str(exit_price),
                "time_in_force": "gtc" if quote["asset"] == "crypto" else "day",
                "client_order_id": client_id,
            }
            if quote["asset"] == "option":
                payload["position_intent"] = "sell_to_close"
            self.submit(client_id, payload, intent["decision"], "exit")

    def enter(self, record, snapshot, state, controls):
        if record["demo"] or record["role"] != "champion" or record["plan"]["action"] != "trade":
            return
        if parse_time(record["expires_at"]) <= utcnow():
            return
        client_id = "mt-e-" + record["id"]
        if any(
            e["payload"]["client_order_id"] == client_id for e in self.store.list("broker_intent")
        ):
            return
        if state["unresolved"]:
            return
        if state["account"].get("account_blocked") or state["account"].get("trading_blocked"):
            return
        owned_symbols = {
            e["payload"]["decision"]["plan"]["symbol"].replace("/", "")
            for e in self.store.list("broker_intent")
            if e["payload"]["intent_type"] == "entry"
        }
        if any(p["symbol"].replace("/", "") not in owned_symbols for p in state["positions"]):
            return  # Dedicated paper account required; do not mix manual exposure.
        if any(
            o["status"] not in TERMINAL
            and not str(o.get("client_order_id", "")).startswith("mt-")
            and o.get("order_class") != "bracket"
            for o in state["orders"]
        ):
            return
        # Only one new network order per reconciliation. Pending orders reserve capacity.
        active_entries = [
            o
            for o in state["orders"]
            if o["status"] not in TERMINAL and str(o.get("client_order_id", "")).startswith("mt-e-")
        ]
        if active_entries:
            return
        qraw = next(
            (q for q in snapshot["quotes"] if q["symbol"] == record["plan"]["symbol"]), None
        )
        if not qraw:
            return
        quote = Quote.model_validate(qraw)
        decision = Decision.model_validate(record["plan"])
        initial = self.store.get("broker_initial_equity")
        gross = sum(abs(float(p["market_value"])) for p in state["positions"])
        account_positions = [
            {
                "symbol": p["symbol"],
                "underlying": p["symbol"],
                "mark": float(p["current_price"]),
                "remaining": abs(float(p["qty"])),
                "multiplier": 100 if p["asset_class"] == "us_option" else 1,
            }
            for p in state["positions"]
        ]
        # Include option underlying exposure, not only the OCC string.
        for position in account_positions:
            for intent in self.store.list("broker_intent"):
                original = intent["payload"]["decision"]
                if original["plan"]["symbol"].replace("/", "") == position["symbol"].replace(
                    "/", ""
                ):
                    position["underlying"] = original["quote"]["underlying"]
                    position["symbol"] = original["plan"]["symbol"]
        account = {
            "equity": min(controls.paper_capital, float(state["account"]["equity"])),
            "cash": min(float(state["account"]["cash"]), max(0, controls.paper_capital - gross)),
            "gross_exposure": gross,
            "positions": account_positions,
            "missing_marks": [],
            "daily_pnl": state["daily_pnl"],
            "total_pnl": float(state["account"]["equity"]) - initial,
        }
        original_snapshot = next(
            (
                e["payload"]
                for e in self.store.list("snapshot")
                if e["payload"]["id"] == record["snapshot_id"]
            ),
            {},
        )
        evidence = {record["quote"]["source_id"], quote.source_id} | {
            n["id"] for n in original_snapshot.get("news", [])
        }
        refreshed = decision.model_copy(
            update={"evidence_ids": decision.evidence_ids + [quote.source_id]}
        )
        gate = validate_trade(refreshed, quote, controls, account, evidence, snapshot["clock"])
        if decision.direction == "short":
            asset = self.get("/v2/assets/" + quote.symbol, paper=True)
            if not asset.get("shortable") or not asset.get("easy_to_borrow"):
                gate["accepted"] = False
                gate["reasons"].append("Broker borrow unavailable")
        if quote.asset == "option" and int(state["account"].get("options_trading_level") or 0) < 2:
            gate["accepted"] = False
            gate["reasons"].append("Bought options approval unavailable")
        self.store.append(
            "gate",
            {"decision_id": record["id"], "book": "alpaca_paper", **gate},
            f"broker-gate:{record['id']}:{quote.source_id}",
        )
        if not gate["accepted"]:
            return
        decimals = 2 if gate["price"] >= 1 else 4
        payload = {
            "symbol": quote.symbol,
            "qty": str(gate["qty"]),
            "side": "buy" if decision.direction == "long" else "sell",
            "type": "limit",
            "limit_price": str(round(gate["price"], decimals)),
            "time_in_force": "gtc" if quote.asset == "crypto" else "day",
            "client_order_id": client_id,
        }
        if quote.asset == "stock":
            payload.update(
                {
                    "order_class": "bracket",
                    "take_profit": {"limit_price": round(decision.target, 2)},
                    "stop_loss": {"stop_price": round(decision.stop, 2)},
                }
            )
        elif quote.asset == "option":
            payload["position_intent"] = "buy_to_open"
        self.submit(client_id, payload, record)

    def cycle(self, engine, snapshot):
        state = self.reconcile()
        if not self.store.lease_valid(self.lease_token):
            raise ProviderError("Execution lease expired during reconciliation")
        for event in self.store.list("broker_intent"):
            intent = event["payload"]
            if intent["intent_type"] != "entry":
                continue
            if parse_time(intent["decision"]["expires_at"]) > utcnow():
                continue
            order = next(
                (
                    o
                    for o in state["orders"]
                    if o.get("client_order_id") == intent["client_order_id"]
                ),
                None,
            )
            if (
                order
                and order["status"] not in TERMINAL
                and float(order.get("filled_qty") or 0) == 0
            ):
                self.request("DELETE", "/v2/orders/" + order["id"])
        cancel_requested = self.store.get("cancel_entries_requested")
        if cancel_requested:
            for order in state["orders"]:
                if (
                    str(order.get("client_order_id", "")).startswith("mt-e-")
                    and order["status"] not in TERMINAL
                    and float(order.get("filled_qty") or 0) == 0
                    and order.get("created_at", "") <= cancel_requested
                ):
                    self.request("DELETE", "/v2/orders/" + order["id"])
        self.manage(state, snapshot)
        controls = engine.controls()
        if controls.mode != "auto" or controls.state != "enabled":
            return
        for event in self.store.list("decision", 300):
            before = len(self.store.list("broker_intent"))
            self.enter(event["payload"], snapshot, state, controls)
            if len(self.store.list("broker_intent")) != before:
                break
