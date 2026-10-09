from __future__ import annotations

import hashlib
import math
import os
import random
import threading
import uuid
from datetime import datetime, timedelta

from .models import Controls, Decision, Quote, timestamp, utcnow
from .providers import (
    AlpacaResearch,
    OpenAIAnalyst,
    ProviderError,
    demo_briefing,
    demo_snapshot,
    version_config,
)


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def multiplier(quote):
    return 100 if quote["asset"] == "option" else 1


def positions(events, book):
    opened = {}
    for event in events:
        p = event["payload"]
        if p.get("book") != book:
            continue
        if event["kind"] == "entry":
            opened[p["position_id"]] = {**p, "remaining": p["qty"]}
        elif event["kind"] == "exit" and p["position_id"] in opened:
            opened[p["position_id"]]["remaining"] -= p["qty"]
    return [p for p in opened.values() if p["remaining"] > 1e-8]


def portfolio(events, book, quotes, capital):
    cash, realized, costs = capital, 0.0, 0.0
    for event in events:
        p = event["payload"]
        if p.get("book") != book:
            continue
        if event["kind"] in {"entry", "exit"}:
            cash += p["cash_delta"]
            costs += p["cost"]
        if event["kind"] == "exit":
            realized += p["pnl"]
    holdings = positions(events, book)
    equity, gross, unrealized, missing_marks = cash, 0.0, 0.0, []
    for p in holdings:
        quote = quotes.get(p["symbol"])
        if not quote:
            missing_marks.append(p["symbol"])
        mark = (quote["bid"] if p["direction"] == "long" else quote["ask"]) if quote else p["fill"]
        sign = 1 if p["direction"] == "long" else -1
        value = p["remaining"] * p["multiplier"] * mark
        equity += sign * value
        gross += value
        unrealized += sign * (mark - p["fill"]) * p["remaining"] * p["multiplier"]
        p["mark"] = mark
        p["unrealized_pnl"] = sign * (mark - p["fill"]) * p["remaining"] * p["multiplier"]
    return {
        "book": book,
        "cash": cash,
        "equity": equity,
        "gross_exposure": gross,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": equity - capital,
        "costs": costs,
        "positions": holdings,
        "missing_marks": missing_marks,
    }


def validate_trade(decision, quote, controls, account, evidence, clock, now=None):
    """Pure risk gate. All rejection reasons persist; no model override."""
    now = now or utcnow()
    reasons = []
    age = (now - quote.observed_at).total_seconds()
    if age < -5 or age > controls.max_quote_age_seconds:
        reasons.append("Quote stale or future-dated")
    if (
        not set(decision.evidence_ids).issubset(evidence)
        or quote.source_id not in decision.evidence_ids
    ):
        reasons.append("Evidence missing, invented, or not linked to this quote")
    if quote.asset not in controls.enabled_assets:
        reasons.append("Asset disabled")
    if quote.asset != "crypto" and (not clock or not clock.get("is_open")):
        reasons.append("Stock/options market closed or clock unavailable")
    if (
        decision.style == "day"
        and quote.asset != "crypto"
        and clock
        and clock.get("next_close")
        and (parse_time(clock["next_close"]) - now).total_seconds() <= 300
    ):
        reasons.append("Too close to session end for new day trade")
    if quote.asset == "option":
        if (
            not quote.expiration
            or not quote.strike
            or quote.delta is None
            or quote.implied_volatility is None
        ):
            reasons.append("Options contract details or Greeks unavailable")
        elif quote.expiration <= (now + timedelta(days=2)).date().isoformat():
            reasons.append("Near-expiration contracts excluded by v1 lifecycle policy")
        if decision.direction != "long":
            reasons.append("Option selling not enabled")
    if decision.direction == "short" and (quote.asset != "stock" or not controls.allow_stock_short):
        reasons.append("Short selling not enabled for this asset")
    spread = (quote.ask - quote.bid) / quote.ask * 10000
    if spread > controls.max_spread_bps:
        reasons.append("Spread exceeds limit")
    if account["missing_marks"]:
        reasons.append("Existing position valuation unavailable")
    if len(account["positions"]) >= controls.max_positions:
        reasons.append("Position count limit reached")
    if any(p["symbol"] == quote.symbol for p in account["positions"]):
        reasons.append("Position already exists; pyramiding disabled")
    if account["daily_pnl"] <= -controls.daily_loss_limit:
        reasons.append("Daily loss limit reached")
    if account["total_pnl"] <= -controls.experiment_loss_limit:
        reasons.append("Cumulative experiment loss limit reached")
    price = quote.ask if decision.direction == "long" else quote.bid
    if not decision.entry_low <= price <= decision.entry_high:
        reasons.append("Entry trigger not reached")
    mult = 100 if quote.asset == "option" else 1
    costs = price * controls.cost_bps_per_side / 10000 * 2
    # Bought options can lose the entire premium; a stop is not a maximum loss.
    risk_unit = (price if quote.asset == "option" else abs(price - decision.stop)) + costs
    equity = max(0, account["equity"])
    underlying_exposure = sum(
        abs(p["mark"] * p["remaining"] * p["multiplier"])
        for p in account["positions"]
        if p["underlying"] == quote.underlying
    )
    position_room = max(0, equity * controls.max_position_pct / 100 - underlying_exposure)
    exposure_room = max(0, equity * controls.max_exposure_pct / 100 - account["gross_exposure"])
    cash_room = max(0, account["cash"]) / (1 + controls.cost_bps_per_side / 10000)
    raw_qty = min(
        controls.risk_per_trade / (risk_unit * mult),
        position_room / (price * mult),
        exposure_room / (price * mult),
        cash_room / (price * mult),
    )
    qty = (
        math.floor(raw_qty * 1000000) / 1000000 if quote.asset == "crypto" else math.floor(raw_qty)
    )
    if qty <= 0:
        reasons.append("Insufficient risk, cash, or exposure capacity for minimum size")
    available = quote.ask_size if decision.direction == "long" else quote.bid_size
    if available < qty:
        reasons.append("Quoted liquidity insufficient for planned quantity")
    return {
        "accepted": not reasons,
        "reasons": reasons,
        "qty": qty,
        "price": price,
        "planned_risk": qty * risk_unit * mult,
        "spread_bps": spread,
        "quote_age_seconds": age,
    }


class Engine:
    def __init__(self, store, research=None, analyst=None):
        self.store = store
        self.research = research or AlpacaResearch()
        self.analyst = analyst or OpenAIAnalyst()
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.scan_event = threading.Event()
        self.owner = str(uuid.uuid4())
        self.threads = []
        self.broker = None
        if os.getenv("EXECUTION_BACKEND", "simulation") == "alpaca_paper":
            from .broker import PaperBroker

            self.broker = PaperBroker(store)
        if self.store.get("controls") is None:
            self.store.set("controls", Controls().model_dump())

    @property
    def demo(self):
        return os.getenv("MASTER_DEMO", "false").lower() == "true"

    def controls(self):
        return Controls.model_validate(self.store.get("controls"))

    def log(self, level, message, **details):
        self.store.append(
            "activity", {"level": level, "message": message, **details}, str(uuid.uuid4())
        )

    def configure(self, controls):
        with self.lock:
            if (
                self.store.list("entry", 1)
                and controls.paper_capital != self.controls().paper_capital
            ):
                raise ValueError(
                    "Capital cannot be changed after trading; use a new isolated database"
                )
            self.store.set("controls", controls.model_dump())
            self.log("info", "Controls updated", controls=controls.model_dump())

    def snapshot(self, persist=True):
        if not self.demo:
            entries = self.store.list("entry")
            ledger = sorted(entries + self.store.list("exit"), key=lambda e: e["seq"])
            books = {e["payload"]["book"] for e in entries}
            held = [p for book in books for p in positions(ledger, book)]
            self.research.extra_symbols = list({(p["symbol"], p["asset"]) for p in held})
            broker_positions = self.store.get("broker_state", {}).get("positions", [])
            for event in self.store.list("broker_intent"):
                original = event["payload"]["decision"]
                symbol = original["plan"]["symbol"]
                if not any(
                    p["symbol"].replace("/", "") == symbol.replace("/", "")
                    for p in broker_positions
                ):
                    continue
                pair = (original["plan"]["symbol"], original["quote"]["asset"])
                if pair not in self.research.extra_symbols:
                    self.research.extra_symbols.append(pair)
        result = demo_snapshot() if self.demo else self.research.snapshot()
        if persist:
            self.store.append("snapshot", result, "snapshot:" + result["id"])
        self.store.set("latest_snapshot", result)
        return result

    def register_versions(self):
        versions = [version_config("champion")]
        if os.getenv("CHALLENGER_INSTRUCTIONS"):
            versions.append(version_config("challenger"))
        for v in versions:
            self.store.append("version", v, "version:" + v["id"])
        return versions

    def account(self, book, snapshot=None):
        snapshot = snapshot or self.store.get("latest_snapshot", {"quotes": []})
        quotes = {q["symbol"]: q for q in snapshot["quotes"]}
        ledger = sorted(self.store.list("entry") + self.store.list("exit"), key=lambda e: e["seq"])
        result = portfolio(ledger, book, quotes, self.controls().paper_capital)
        # UTC boundaries are explicit; first valuation of a day is its baseline.
        key = "day_start:" + book + ":" + utcnow().date().isoformat()
        baseline = self.store.get(key)
        if baseline is None:
            previous = [
                e["payload"]["equity"]
                for e in self.store.list("equity")
                if e["payload"]["book"] == book
                and e["payload"]["at"][:10] < utcnow().date().isoformat()
            ]
            baseline = previous[-1] if previous else self.controls().paper_capital
            self.store.set(key, baseline)
        result["daily_pnl"] = result["equity"] - baseline
        return result

    def book(self, version):
        return ("demo:" if self.demo else "paper:") + version

    def scan(self):
        if not self.scan_lock.acquire(blocking=False):
            return
        token = self.store.acquire(self.owner, seconds=300, key="analysis_lease")
        if not token:
            self.scan_lock.release()
            self.log("warning", "Scan skipped: another instance owns the analysis lease")
            return
        try:
            self.store.set("scan_status", {"state": "collecting", "started_at": timestamp()})
            snapshot = self.snapshot()
            if not snapshot["quotes"]:
                raise ProviderError("No valid market quotes; research cannot proceed")
            self.log(
                "info",
                "Candidate pool captured before selection",
                candidates=len(snapshot["quotes"]),
            )
            journal = [e["payload"] for e in self.store.list("exit", 100)]
            versions = self.register_versions()
            for version in versions:
                budget_key = "ai_calls:" + utcnow().date().isoformat()
                calls = self.store.get(budget_key, 0)
                if not self.demo:
                    if calls >= int(os.getenv("MAX_AI_CALLS_PER_DAY", "96")):
                        raise ProviderError(
                            "Daily AI request budget exhausted; position monitoring continues"
                        )
                    self.store.set(budget_key, calls + 1)
                self.store.set(
                    "scan_status",
                    {"state": "analyzing", "version": version["id"], "started_at": timestamp()},
                )
                briefing, usage = (
                    (demo_briefing(snapshot), {})
                    if self.demo
                    else self.analyst.analyze(snapshot, version, journal)
                )
                decisions = {d.symbol: d for d in briefing.decisions}
                expected = {q["symbol"] for q in snapshot["quotes"]}
                if set(decisions) != expected or len(briefing.decisions) != len(expected):
                    raise ProviderError("Candidate classification incomplete or duplicated")
                record = {
                    "snapshot_id": snapshot["id"],
                    "version": version["id"],
                    "demo": self.demo,
                    **briefing.model_dump(mode="json"),
                    "api": usage,
                }
                self.store.append("briefing", record, f"brief:{snapshot['id']}:{version['id']}")
                for q in snapshot["quotes"]:
                    d = decisions[q["symbol"]]
                    pid = hashlib.sha256(
                        f"{snapshot['id']}:{version['id']}:{d.symbol}".encode()
                    ).hexdigest()[:24]
                    self.store.append(
                        "decision",
                        {
                            "id": pid,
                            "version": version["id"],
                            "role": version["role"],
                            "snapshot_id": snapshot["id"],
                            "demo": self.demo,
                            "created_at": timestamp(),
                            "quote": q,
                            "plan": d.model_dump(mode="json"),
                            "expires_at": (
                                utcnow() + timedelta(minutes=d.valid_minutes)
                            ).isoformat(),
                        },
                        "decision:" + pid,
                    )
            self.record_controls(snapshot)
            self.store.set("scan_status", {"state": "idle", "completed_at": timestamp()})
            self.log(
                "info", "Research complete; all entries and passes recorded", versions=len(versions)
            )
        except Exception as exc:
            self.store.set(
                "scan_status", {"state": "error", "at": timestamp(), "error": str(exc)[:300]}
            )
            self.log("error", "Research failed; no fallback trades", error=str(exc)[:300])
        finally:
            self.store.release(token, key="analysis_lease")
            self.scan_lock.release()

    def record_controls(self, snapshot):
        """Reference experiment: identical fixed exit policy for every candidate.

        Separate diagnostic books. Never mixed into AI portfolio or live approval.
        """
        rng = random.Random(snapshot["id"])
        selected = set(
            rng.sample([q["symbol"] for q in snapshot["quotes"]], min(3, len(snapshot["quotes"])))
        )
        champion = version_config("champion")["id"]
        entries = {
            e["payload"]["plan"]["symbol"]
            for e in self.store.list("decision", 100)
            if e["payload"]["snapshot_id"] == snapshot["id"]
            and e["payload"]["version"] == champion
            and e["payload"]["plan"]["action"] == "trade"
        }
        for q in snapshot["quotes"]:
            price = q["ask"]
            plan = Decision(
                symbol=q["symbol"],
                action="trade",
                direction="long",
                style="swing",
                entry_low=price * 0.995,
                entry_high=price * 1.005,
                stop=price * 0.98,
                target=price * 1.04,
                valid_minutes=15,
                holding_hours=24,
                evidence_ids=[q["source_id"]],
                thesis="Predeclared reference protocol; no AI exit selection.",
                invalidation="Reference stop breached.",
                counterargument="Diagnostic only; not evidence of live qualification.",
                alternative="Hold cash in the control account.",
            )
            groups = ["pass_reference" if q["symbol"] not in entries else "entry_reference"]
            if q["symbol"] in selected:
                groups.append("random_reference")
            for group in groups:
                pid = hashlib.sha256(
                    f"{snapshot['id']}:{group}:{q['symbol']}".encode()
                ).hexdigest()[:24]
                self.store.append(
                    "decision",
                    {
                        "id": pid,
                        "version": group,
                        "role": "reference",
                        "snapshot_id": snapshot["id"],
                        "demo": self.demo,
                        "created_at": timestamp(),
                        "quote": q,
                        "plan": plan.model_dump(mode="json"),
                        "expires_at": (utcnow() + timedelta(minutes=15)).isoformat(),
                    },
                    "decision:" + pid,
                )

    def enter(self, record, snapshot):
        d = Decision.model_validate(record["plan"])
        if d.action != "trade" or record["demo"] != self.demo:
            return
        if parse_time(record["expires_at"]) <= utcnow():
            return
        book = self.book(record["version"])
        if any(e["payload"].get("position_id") == record["id"] for e in self.store.list("entry")):
            return
        qraw = next((q for q in snapshot["quotes"] if q["symbol"] == d.symbol), None)
        if qraw is None:
            return
        q = Quote.model_validate(qraw)
        evidence = {n["id"] for n in snapshot["news"]} | {q.source_id, record["quote"]["source_id"]}
        # Validate the original immutable evidence, while refreshing executable price.
        original_snapshot = next(
            (
                e["payload"]
                for e in self.store.list("snapshot")
                if e["payload"]["id"] == record["snapshot_id"]
            ),
            None,
        )
        if original_snapshot:
            evidence |= {n["id"] for n in original_snapshot["news"]}
        refreshed = d.model_copy(update={"evidence_ids": d.evidence_ids + [q.source_id]})
        account = self.account(book, snapshot)
        gate = validate_trade(refreshed, q, self.controls(), account, evidence, snapshot["clock"])
        self.store.append(
            "gate",
            {"decision_id": record["id"], "book": book, **gate},
            f"gate:{record['id']}:{q.source_id}",
        )
        if not gate["accepted"]:
            return
        mult = multiplier(qraw)
        sign = 1 if d.direction == "long" else -1
        cost = gate["qty"] * gate["price"] * mult * self.controls().cost_bps_per_side / 10000
        payload = {
            "book": book,
            "position_id": record["id"],
            "symbol": d.symbol,
            "asset": q.asset,
            "underlying": q.underlying,
            "direction": d.direction,
            "style": d.style,
            "qty": gate["qty"],
            "fill": gate["price"],
            "multiplier": mult,
            "stop": d.stop,
            "target": d.target,
            "opened_at": timestamp(),
            "exit_by": (utcnow() + timedelta(hours=d.holding_hours)).isoformat(),
            "expiration": q.expiration,
            "cost": cost,
            "cash_delta": -sign * gate["qty"] * gate["price"] * mult - cost,
            "planned_risk": gate["planned_risk"],
            "source_id": q.source_id,
            "execution": "local quote-based simulation",
            "demo": self.demo,
        }
        self.store.append("entry", payload, "entry:" + record["id"])
        self.log("info", "Simulated entry filled", symbol=d.symbol, book=book, qty=gate["qty"])

    def manage(self, snapshot):
        events = sorted(self.store.list("entry") + self.store.list("exit"), key=lambda e: e["seq"])
        books = {e["payload"]["book"] for e in events if e["kind"] == "entry"}
        quotes = {q["symbol"]: q for q in snapshot["quotes"]}
        for book in books:
            if not book.startswith("demo:" if self.demo else "paper:"):
                continue
            for p in positions(events, book):
                q = quotes.get(p["symbol"])
                if not q:
                    continue
                age = (utcnow() - parse_time(q["observed_at"])).total_seconds()
                if age < -5 or age > self.controls().max_quote_age_seconds:
                    continue
                clock = snapshot.get("clock")
                if p["asset"] != "crypto" and (not clock or not clock.get("is_open")):
                    continue
                sign = 1 if p["direction"] == "long" else -1
                price = q["bid"] if sign == 1 else q["ask"]
                reason = None
                requested = self.store.get("close_requested")
                if requested and p["opened_at"] <= requested:
                    reason = "owner_close"
                elif sign * (price - p["stop"]) <= 0:
                    reason = "stop"
                elif sign * (price - p["target"]) >= 0:
                    reason = "target"
                elif parse_time(p["exit_by"]) <= utcnow():
                    reason = "time_exit"
                elif (
                    p["asset"] == "option"
                    and p["expiration"] <= (utcnow() + timedelta(days=1)).date().isoformat()
                ):
                    reason = "before_expiration"
                elif p["style"] == "day" and clock and clock.get("next_close"):
                    if (parse_time(clock["next_close"]) - utcnow()).total_seconds() <= 300:
                        reason = "session_close"
                elif (
                    p["style"] == "day"
                    and p["asset"] == "crypto"
                    and parse_time(p["opened_at"]).date() < utcnow().date()
                ):
                    reason = "utc_day_close"
                if not reason:
                    continue
                available = q["bid_size"] if sign == 1 else q["ask_size"]
                qty = min(p["remaining"], available)
                if p["asset"] != "crypto":
                    qty = math.floor(qty)
                if qty <= 0:
                    continue
                cost = qty * price * p["multiplier"] * self.controls().cost_bps_per_side / 10000
                entry_cost = p["cost"] * qty / p["qty"]
                self.store.append(
                    "exit",
                    {
                        "book": book,
                        "position_id": p["position_id"],
                        "symbol": p["symbol"],
                        "asset": p["asset"],
                        "qty": qty,
                        "fill": price,
                        "reason": reason,
                        "cost": cost,
                        "closed_at": timestamp(),
                        "cash_delta": sign * qty * price * p["multiplier"] - cost,
                        "pnl": sign * (price - p["fill"]) * qty * p["multiplier"]
                        - cost
                        - entry_cost,
                        "source_id": q["source_id"],
                        "demo": self.demo,
                    },
                    f"exit:{p['position_id']}:{q['source_id']}",
                )
                self.log(
                    "info", "Simulated position exit", symbol=p["symbol"], book=book, reason=reason
                )
            account = self.account(book, snapshot)
            self.store.append(
                "equity",
                {"at": timestamp(), **{k: v for k, v in account.items() if k != "positions"}},
                f"equity:{book}:{snapshot['id']}",
            )

    def tick(self):
        with self.lock:
            token = self.store.acquire(self.owner, seconds=300)
            if not token:
                return
            try:
                snapshot = self.snapshot(persist=False)
                self.manage(snapshot)  # exits run even when entries are paused
                controls = self.controls()
                if controls.state == "enabled" and controls.mode == "auto":
                    for event in self.store.list("decision", 1000):
                        if not self.store.lease_valid(token):
                            break
                        record = event["payload"]
                        if (
                            record["role"] == "champion"
                            and record["version"] != version_config("champion")["id"]
                        ):
                            continue
                        self.enter(record, snapshot)
                if self.broker and not self.demo:
                    self.broker.lease_token = token
                    self.broker.cycle(self, snapshot)
                self.store.set(
                    "heartbeat",
                    {
                        "at": timestamp(),
                        "owner": self.owner,
                        "quotes": len(snapshot["quotes"]),
                        "errors": snapshot["errors"],
                    },
                )
            except Exception as exc:
                self.log(
                    "error", "Monitor failed; new entries blocked this cycle", error=str(exc)[:300]
                )
                self.store.set("heartbeat", {"at": timestamp(), "error": str(exc)[:300]})
            finally:
                self.store.release(token)

    def start(self):
        def monitor():
            while not self.stop_event.is_set():
                self.tick()
                self.stop_event.wait(max(10, int(os.getenv("MONITOR_SECONDS", "30"))))

        def researcher():
            interval = max(300, int(os.getenv("SCAN_SECONDS", "900")))
            while not self.stop_event.is_set():
                self.scan()
                self.scan_event.wait(interval)
                self.scan_event.clear()

        for target in (monitor, researcher):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.stop_event.set()
        self.scan_event.set()

    def dashboard(self):
        snapshot = self.store.get(
            "latest_snapshot", {"quotes": [], "news": [], "errors": [], "clock": None}
        )
        events = self.store.list("entry")
        books = {e["payload"]["book"] for e in events if e["kind"] == "entry"}
        accounts = [self.account(b, snapshot) for b in sorted(books)]
        versions = [e["payload"] for e in self.store.list("version")]
        decisions = [e["payload"] for e in self.store.list("decision", 150)]
        gates = [e["payload"] for e in self.store.list("gate", 150)]
        all_exits = [e["payload"] for e in self.store.list("exit")]
        exits = all_exits[-150:]
        all_samples = [e["payload"] for e in self.store.list("equity")]
        samples = all_samples[-500:]
        comparison = []
        for a in accounts:
            closed = [e for e in all_exits if e["book"] == a["book"]]
            series = [s["equity"] for s in all_samples if s["book"] == a["book"]]
            peak = self.controls().paper_capital
            dd = 0.0
            for value in series:
                peak = max(peak, value)
                dd = min(dd, (value - peak) / peak * 100)
            comparison.append(
                {
                    "book": a["book"],
                    "net_pnl": a["total_pnl"],
                    "exits": len(closed),
                    "win_rate": sum(e["pnl"] > 0 for e in closed) / len(closed) if closed else None,
                    "drawdown_pct": dd,
                    "status": "Descriptive only — no statistical qualification",
                }
            )
        lessons = []
        for reason in {e["reason"] for e in exits}:
            rows = [
                e
                for e in exits
                if e["reason"] == reason and not e["demo"] and "reference" not in e["book"]
            ]
            if rows:
                lessons.append(
                    {
                        "observation": f"{reason}: {len(rows)} exit fills, net P&L ${sum(r['pnl'] for r in rows):.2f}",
                        "status": "Observation only; partial exits are not independent trades",
                    }
                )
        return {
            "at": timestamp(),
            "demo": self.demo,
            "execution": "Alpaca paper broker + diagnostic simulations"
            if self.broker and not self.demo
            else "Local quote-based paper simulation",
            "broker": self.store.get("broker_state") if self.broker and not self.demo else None,
            "live_enabled": False,
            "controls": self.controls().model_dump(),
            "heartbeat": self.store.get("heartbeat"),
            "scan": self.store.get("scan_status", {}),
            "connections": {
                "alpaca": self.research.ready(),
                "openai": bool(os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_MODEL")),
                "database": "postgres"
                if "postgres" in str(self.store.engine.url)
                else "sqlite/local",
            },
            "snapshot": snapshot,
            "accounts": accounts,
            "decisions": decisions,
            "gates": gates,
            "briefings": [e["payload"] for e in self.store.list("briefing", 20)],
            "versions": [
                {k: v for k, v in version.items() if k != "prompt"} for version in versions
            ],
            "journal": exits,
            "equity": samples,
            "comparison": comparison,
            "lessons": lessons,
            "activity": [
                {"at": e["created_at"], **e["payload"]} for e in self.store.list("activity", 80)
            ],
            "capabilities": {
                "stock": "long/optional short simulation",
                "crypto": "long simulation",
                "option": "bought call/put simulation and paper execution; bounded chain discovery",
                "broker_orders": "Alpaca PAPER-only, enabled"
                if self.broker
                else "Disabled; set EXECUTION_BACKEND=alpaca_paper to connect paper execution",
                "promotion": "Evidence recorded; automatic promotion disabled",
                "sentiment": "AI interpretation of supplied news; no social feed",
                "limits": "Paper defaults; owner must configure before auto",
                "evaluation": "Reference controls are diagnostic; confirmatory experiment not yet registered",
            },
        }
