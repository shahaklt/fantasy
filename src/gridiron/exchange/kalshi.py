"""Kalshi trade-API client (REST + WebSocket).

Authentication is an RSA-PSS signature over ``timestamp_ms + METHOD + path``
(path includes the ``/trade-api/v2`` prefix and excludes the query string),
sent as three headers: ``KALSHI-ACCESS-KEY``, ``KALSHI-ACCESS-TIMESTAMP`` and
``KALSHI-ACCESS-SIGNATURE``. Public market data needs no signature at all, so
the read-only half of this client works with no credentials configured.

Kalshi quotes in cents; everything here is converted to probability units on
the way in and back to cents on the way out, so the rest of the app never has
to think about it.

.. warning::
   The base host has moved more than once. ``GRIDIRON_KALSHI_BASE`` overrides
   it, and :meth:`KalshiClient.probe_hosts` will find the one that answers.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from ..config import USER_DIR
from .base import Action, Market, Order, OrderType, Position, Side
from ..quant.microstructure import BookLevel, OrderBook

log = logging.getLogger(__name__)

DEFAULT_HOSTS = (
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://trading-api.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2",
)
DEMO_HOST = "https://demo-api.kalshi.co/trade-api/v2"
WS_HOSTS = (
    "wss://api.elections.kalshi.com/trade-api/ws/v2",
    "wss://trading-api.kalshi.com/trade-api/ws/v2",
)
#: NFL contracts live under series tickers beginning with these prefixes.
NFL_SERIES_PREFIXES = ("KXNFL", "NFL")

CREDENTIALS_PATH = USER_DIR / "kalshi_credentials.json"


@dataclass
class KalshiCredentials:
    """Key id plus a PEM private key. Loaded from the environment or a local file."""

    key_id: str
    private_key_pem: str

    @classmethod
    def load(cls) -> "KalshiCredentials | None":
        key_id = os.environ.get("KALSHI_KEY_ID", "").strip()
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
        key_pem = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()

        if key_id and key_path and Path(key_path).exists():
            return cls(key_id, Path(key_path).read_text())
        if key_id and key_pem:
            return cls(key_id, key_pem.replace("\\n", "\n"))

        if CREDENTIALS_PATH.exists():
            try:
                data = json.loads(CREDENTIALS_PATH.read_text())
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read Kalshi credentials: %s", exc)
                return None
            kid = data.get("key_id", "")
            pem = data.get("private_key", "")
            path = data.get("private_key_path", "")
            if kid and not pem and path and Path(path).exists():
                pem = Path(path).read_text()
            if kid and pem:
                return cls(kid, pem)
        return None

    @staticmethod
    def save(key_id: str, private_key_path: str) -> Path:
        """Persist a credential *reference* locally (never the key material itself)."""
        CREDENTIALS_PATH.write_text(json.dumps(
            {"key_id": key_id, "private_key_path": private_key_path}, indent=2))
        try:
            CREDENTIALS_PATH.chmod(0o600)
        except OSError:
            pass
        return CREDENTIALS_PATH


class KalshiClient:
    """Read-only by default; trading methods require credentials."""

    venue = "kalshi"

    def __init__(self, credentials: KalshiCredentials | None = None,
                 base_url: str | None = None, demo: bool = False, timeout: int = 20):
        self.credentials = credentials or KalshiCredentials.load()
        env_base = os.environ.get("GRIDIRON_KALSHI_BASE", "").strip()
        self.base_url = (base_url or env_base or (DEMO_HOST if demo else DEFAULT_HOSTS[0])).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "gridiron/1.0"
        self._signer = None

    # ------------------------------------------------------------------ auth
    @property
    def authenticated(self) -> bool:
        return self.credentials is not None

    def _load_signer(self):
        if self._signer is not None:
            return self._signer
        if not self.credentials:
            raise RuntimeError(
                "Kalshi credentials are not configured. Set KALSHI_KEY_ID and "
                "KALSHI_PRIVATE_KEY_PATH, or write data/user/kalshi_credentials.json."
            )
        from cryptography.hazmat.primitives import serialization

        self._signer = serialization.load_pem_private_key(
            self.credentials.private_key_pem.encode(), password=None)
        return self._signer

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """RSA-PSS(SHA-256, MGF1-SHA256, salt=32) over timestamp+METHOD+path."""
        # Resolve credentials before importing the crypto stack, so a missing
        # key reports "no credentials" rather than an import error.
        key = self._load_signer()

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        message = f"{timestamp_ms}{method.upper()}{path}".encode()
        signature = key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    def _headers(self, method: str, path: str) -> dict:
        if not self.authenticated:
            return {}
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.credentials.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, path),
            "Content-Type": "application/json",
        }

    def _request(self, method: str, endpoint: str, params: dict | None = None,
                 body: dict | None = None, auth: bool = False) -> dict:
        from urllib.parse import urlparse

        url = f"{self.base_url}{endpoint}"
        path = urlparse(url).path  # signature covers the path only, no query
        headers = self._headers(method, path) if auth else {}
        resp = self.session.request(method, url, params=params, json=body,
                                    headers=headers, timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Kalshi {method} {endpoint} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}

    @classmethod
    def probe_hosts(cls, timeout: int = 8) -> str | None:
        """Return the first Kalshi host that answers, or None if none do."""
        for host in DEFAULT_HOSTS:
            try:
                r = requests.get(f"{host}/exchange/status", timeout=timeout,
                                 headers={"User-Agent": "gridiron/1.0"})
                if r.status_code < 400:
                    return host
            except Exception:  # noqa: BLE001
                continue
        return None

    # ----------------------------------------------------------- market data
    @staticmethod
    def _to_market(raw: dict) -> Market:
        def cents(v):
            return None if v is None else float(v) / 100.0

        return Market(
            venue="kalshi",
            market_id=raw.get("ticker", ""),
            ticker=raw.get("ticker", ""),
            title=raw.get("title") or raw.get("subtitle") or raw.get("ticker", ""),
            event_id=raw.get("event_ticker", ""),
            yes_bid=cents(raw.get("yes_bid")),
            yes_ask=cents(raw.get("yes_ask")),
            last_price=cents(raw.get("last_price")),
            volume=float(raw.get("volume") or 0.0),
            open_interest=float(raw.get("open_interest") or 0.0),
            close_time=raw.get("close_time", ""),
            status=raw.get("status", "open"),
            metadata={k: raw.get(k) for k in ("series_ticker", "expiration_time",
                                              "yes_sub_title", "no_sub_title", "result")},
        )

    def list_markets(self, query: str | None = None, limit: int = 200,
                     series_ticker: str | None = None, status: str = "open") -> list[Market]:
        params: dict = {"limit": min(limit, 1000), "status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        data = self._request("GET", "/markets", params=params)
        markets = [self._to_market(m) for m in data.get("markets", [])]
        if query:
            q = query.lower()
            markets = [m for m in markets
                       if q in m.title.lower() or q in m.ticker.lower()]
        return markets

    def list_nfl_markets(self, limit: int = 500) -> list[Market]:
        """Every open contract whose series looks like an NFL series."""
        out: list[Market] = []
        for prefix in NFL_SERIES_PREFIXES:
            try:
                out.extend(self.list_markets(limit=limit, series_ticker=prefix))
            except Exception as exc:  # noqa: BLE001
                log.debug("kalshi series %s: %s", prefix, exc)
        if not out:
            try:
                out = [m for m in self.list_markets(limit=limit)
                       if m.ticker.upper().startswith(NFL_SERIES_PREFIXES)]
            except Exception as exc:  # noqa: BLE001
                log.info("kalshi market list unavailable: %s", exc)
        seen, unique = set(), []
        for m in out:
            if m.ticker not in seen:
                seen.add(m.ticker)
                unique.append(m)
        return unique

    def get_market(self, market_id: str) -> Market | None:
        data = self._request("GET", f"/markets/{market_id}")
        raw = data.get("market")
        return self._to_market(raw) if raw else None

    def get_orderbook(self, market_id: str, depth: int = 10) -> OrderBook:
        data = self._request("GET", f"/markets/{market_id}/orderbook",
                             params={"depth": depth})
        book = data.get("orderbook", {}) or {}
        # Kalshi returns [[price_cents, size], ...] for each side, and the "no"
        # side is quoted in its own units -- a no bid at 40c is a yes ask at 60c.
        yes = book.get("yes") or []
        no = book.get("no") or []
        bids = [BookLevel(price=float(p) / 100.0, size=float(s)) for p, s in yes]
        asks = [BookLevel(price=1.0 - float(p) / 100.0, size=float(s)) for p, s in no]
        bids.sort(key=lambda l: -l.price)
        asks.sort(key=lambda l: l.price)
        return OrderBook(bids=bids, asks=asks, timestamp=time.time())

    # ---------------------------------------------------------------- trading
    def place_order(self, order: Order) -> Order:
        """Submit a live order. Requires credentials and a real intent to trade."""
        body = {
            "ticker": order.market_id,
            "client_order_id": order.client_id or order.order_id,
            "side": order.side.value,
            "action": order.action.value,
            "count": int(order.quantity),
            "type": "limit" if order.order_type == OrderType.LIMIT else "market",
        }
        if order.order_type == OrderType.LIMIT:
            price_cents = int(round(order.price * 100))
            body["yes_price" if order.side == Side.YES else "no_price"] = (
                price_cents if order.side == Side.YES else 100 - price_cents)
        data = self._request("POST", "/portfolio/orders", body=body, auth=True)
        resp = data.get("order", {})
        order.status = resp.get("status", "submitted")
        order.client_id = resp.get("order_id", order.client_id)
        return order

    def cancel_order(self, order_id: str) -> bool:
        self._request("DELETE", f"/portfolio/orders/{order_id}", auth=True)
        return True

    def positions(self) -> list[Position]:
        data = self._request("GET", "/portfolio/positions", auth=True)
        out = []
        for p in data.get("market_positions", []):
            qty = float(p.get("position", 0))
            if qty == 0:
                continue
            out.append(Position(
                venue="kalshi", market_id=p.get("ticker", ""), ticker=p.get("ticker", ""),
                side=Side.YES if qty > 0 else Side.NO, quantity=abs(qty),
                avg_price=abs(float(p.get("market_exposure", 0.0))) / max(abs(qty), 1) / 100.0,
                realised_pnl=float(p.get("realized_pnl", 0.0)) / 100.0,
            ))
        return out

    def balance(self) -> float:
        data = self._request("GET", "/portfolio/balance", auth=True)
        return float(data.get("balance", 0.0)) / 100.0

    def fills(self, limit: int = 100) -> list[dict]:
        data = self._request("GET", "/portfolio/fills", params={"limit": limit}, auth=True)
        return data.get("fills", [])

    # -------------------------------------------------------------- streaming
    def websocket_url(self) -> str:
        return os.environ.get("GRIDIRON_KALSHI_WS", WS_HOSTS[0])

    def subscribe_message(self, tickers: list[str],
                          channels: tuple[str, ...] = ("orderbook_delta", "ticker_v2", "trade")) -> str:
        """Subscription frame for the live feed.

        Subscribing to ``orderbook_delta`` delivers a snapshot first and then
        incremental updates, which is what the sentiment panel replays into its
        microstructure features.
        """
        return json.dumps({
            "id": 1,
            "cmd": "subscribe",
            "params": {"channels": list(channels), "market_tickers": tickers},
        })
