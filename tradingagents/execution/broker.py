"""Alpaca paper broker adapter. PAPER-ONLY by construction.

The base URL is a class constant. There is deliberately no parameter, env
var, or config entry that can redirect this adapter at the live endpoint.
Authentication happens against the paper endpoint; Alpaca rejects live keys
there, so a live keypair fails closed at connect() time.

Raw REST (no SDK) keeps the order path small and auditable. Order
submission is never retried automatically: on an ambiguous failure the
caller reconciles via GET (orders/positions) before deciding, so a retry
can never double-submit.
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)


class BrokerError(Exception):
    """Anything the broker refused or failed to answer."""


class BrokerAuthError(BrokerError):
    """Credentials missing or rejected -- fail closed."""


class AlpacaPaperBroker:
    PAPER_BASE_URL = "https://paper-api.alpaca.markets"
    DATA_BASE_URL = "https://data.alpaca.markets"

    def __init__(
        self,
        key_id: str | None = None,
        secret_key: str | None = None,
        timeout: int = 15,
    ) -> None:
        from tradingagents.execution import config as cfg

        self.key_id = key_id or os.environ.get(cfg.APCA_KEY_ENV, "")
        self.secret_key = secret_key or os.environ.get(cfg.APCA_SECRET_ENV, "")
        self.timeout = timeout
        if not self.key_id or not self.secret_key:
            raise BrokerAuthError(
                f"Alpaca paper credentials are not set "
                f"({cfg.APCA_KEY_ENV}/{cfg.APCA_SECRET_ENV})"
            )

    # -- low-level -------------------------------------------------------
    def _headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, url: str, **kwargs):
        try:
            resp = requests.request(
                method, url, headers=self._headers(),
                timeout=self.timeout, **kwargs,
            )
        except requests.RequestException as exc:
            raise BrokerError(f"{method} {url} failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise BrokerAuthError(
                f"{method} {url} rejected credentials (HTTP {resp.status_code})"
            )
        if not resp.ok:
            raise BrokerError(
                f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json() if resp.text else {}

    def _paper(self, method: str, path: str, **kwargs):
        return self._request(method, self.PAPER_BASE_URL + path, **kwargs)

    # -- account / market state ------------------------------------------
    def connect(self) -> dict:
        """Authenticate against the PAPER endpoint and return the account.

        Raises BrokerAuthError when the keys are rejected (e.g. live keys,
        which do not authenticate on the paper endpoint).
        """
        account = self.get_account()
        if account.get("account_blocked") or account.get("trading_blocked"):
            raise BrokerError("Account is blocked; refusing to trade")
        num = str(account.get("account_number", ""))
        logger.info(
            "Connected to Alpaca PAPER account ****%s (equity %s)",
            num[-4:], account.get("equity"),
        )
        return account

    def get_account(self) -> dict:
        return self._paper("GET", "/v2/account")

    def get_positions(self) -> list:
        return self._paper("GET", "/v2/positions")

    def get_open_orders(self) -> list:
        return self._paper("GET", "/v2/orders", params={"status": "open", "limit": 500})

    def get_clock(self) -> dict:
        return self._paper("GET", "/v2/clock")

    def get_latest_ask(self, symbol: str) -> float:
        """Latest ask price (what a market buy would roughly pay)."""
        data = self._request(
            "GET", f"{self.DATA_BASE_URL}/v2/stocks/{symbol}/quotes/latest"
        )
        quote = (data or {}).get("quote") or {}
        ask = quote.get("ap")
        if not ask:
            raise BrokerError(f"No ask price available for {symbol}")
        return float(ask)

    # -- orders -----------------------------------------------------------
    def submit_bracket_order(
        self,
        symbol: str,
        qty: int,
        client_order_id: str,
        stop_price: float,
        take_profit_price: float,
        side: str = "buy",
    ) -> dict:
        """Submit a market-entry bracket: entry + stop-loss + take-profit.

        Never retried by this method (see module docstring).
        """
        payload = {
            "symbol": symbol,
            "qty": str(int(qty)),
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "client_order_id": client_order_id,
            "take_profit": {"limit_price": round(take_profit_price, 2)},
            "stop_loss": {"stop_price": round(stop_price, 2)},
        }
        order = self._paper("POST", "/v2/orders", json=payload)
        logger.info(
            "Submitted bracket %s %s x%d (client_order_id=%s)",
            side, symbol, qty, client_order_id,
        )
        return order

    def cancel_order(self, order_id: str) -> None:
        self._paper("DELETE", f"/v2/orders/{order_id}")

    def cancel_all_orders(self) -> None:
        self._paper("DELETE", "/v2/orders")

    def close_position(self, symbol: str) -> None:
        self._paper("DELETE", f"/v2/positions/{symbol}")

    def close_all_positions(self) -> None:
        self._paper("DELETE", "/v2/positions", params={"cancel_orders": "true"})
