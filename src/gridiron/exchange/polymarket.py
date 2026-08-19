"""Polymarket client: Gamma (discovery) + CLOB (books and orders).

Market data is public and needs no wallet. Order placement is signed with the
account's key via the official ``py-clob-client``; this module never implements
its own EIP-712 signing, because getting that subtly wrong is how people lose
funds. If the client library is not installed, trading raises a clear error and
everything read-only still works.

Polymarket quotes each outcome as its own ERC-1155 token priced in USDC between
0 and 1, so prices are already probability units.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from ..config import USER_DIR
from ..quant.microstructure import BookLevel, OrderBook
from .base import Action, Market, Order, OrderType, Position, Side

log = logging.getLogger(__name__)

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137
CREDENTIALS_PATH = USER_DIR / "polymarket_credentials.json"

NFL_KEYWORDS = ("nfl", "super bowl", "afc", "nfc", "touchdown", "quarterback")


@dataclass
class PolymarketCredentials:
    """Wallet key plus optional proxy/funder address."""

    private_key: str
    funder: str = ""
    signature_type: int = 0   # 0 EOA, 1 email/magic, 2 browser-wallet proxy

    @classmethod
    def load(cls) -> "PolymarketCredentials | None":
        key = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
        funder = os.environ.get("POLYMARKET_FUNDER", "").strip()
        sig = os.environ.get("POLYMARKET_SIGNATURE_TYPE", "").strip()
        if key:
            return cls(key, funder, int(sig) if sig.isdigit() else 0)
        if CREDENTIALS_PATH.exists():
            try:
                data = json.loads(CREDENTIALS_PATH.read_text())
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read Polymarket credentials: %s", exc)
                return None
            key = data.get("private_key", "")
            if key:
                return cls(key, data.get("funder", ""), int(data.get("signature_type", 0)))
        return None


class PolymarketClient:
    venue = "polymarket"

    def __init__(self, credentials: PolymarketCredentials | None = None,
                 clob_host: str | None = None, gamma_host: str | None = None,
                 timeout: int = 20):
        self.credentials = credentials or PolymarketCredentials.load()
        self.clob_host = (clob_host or os.environ.get("GRIDIRON_POLYMARKET_CLOB", CLOB_HOST)).rstrip("/")
        self.gamma_host = (gamma_host or os.environ.get("GRIDIRON_POLYMARKET_GAMMA", GAMMA_HOST)).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "gridiron/1.0"
        self._clob = None

    @property
    def authenticated(self) -> bool:
        return self.credentials is not None

    def _get(self, host: str, path: str, params: dict | None = None):
        r = self.session.get(f"{host}{path}", params=params, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"Polymarket GET {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    # ----------------------------------------------------------- market data
    def _to_markets(self, raw: dict) -> list[Market]:
        """Gamma returns one row per market with a list of outcome tokens."""
        try:
            tokens = raw.get("clobTokenIds")
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            outcomes = raw.get("outcomes")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            prices = raw.get("outcomePrices")
            if isinstance(prices, str):
                prices = json.loads(prices)
        except Exception:  # noqa: BLE001
            return []
        tokens = tokens or []
        outcomes = outcomes or []
        prices = prices or []

        out = []
        for i, token in enumerate(tokens):
            price = float(prices[i]) if i < len(prices) else None
            label = outcomes[i] if i < len(outcomes) else f"outcome {i}"
            out.append(Market(
                venue="polymarket",
                market_id=str(token),
                ticker=str(raw.get("slug", "")) + f"#{label}",
                title=f"{raw.get('question', '')} - {label}",
                event_id=str(raw.get("conditionId", "")),
                last_price=price,
                volume=float(raw.get("volumeNum") or raw.get("volume") or 0.0),
                open_interest=float(raw.get("liquidityNum") or raw.get("liquidity") or 0.0),
                close_time=str(raw.get("endDate", "")),
                status="open" if raw.get("active") and not raw.get("closed") else "closed",
                metadata={"question": raw.get("question"), "outcome": label,
                          "slug": raw.get("slug"), "condition_id": raw.get("conditionId")},
            ))
        return out

    def list_markets(self, query: str | None = None, limit: int = 200) -> list[Market]:
        params = {"limit": min(limit, 500), "active": "true", "closed": "false"}
        if query:
            params["slug"] = query
        data = self._get(self.gamma_host, "/markets", params=params)
        rows = data if isinstance(data, list) else data.get("data", [])
        markets: list[Market] = []
        for raw in rows:
            markets.extend(self._to_markets(raw))
        if query:
            q = query.lower()
            markets = [m for m in markets if q in m.title.lower() or q in m.ticker.lower()]
        return markets

    def list_nfl_markets(self, limit: int = 500) -> list[Market]:
        try:
            markets = self.list_markets(limit=limit)
        except Exception as exc:  # noqa: BLE001
            log.info("polymarket market list unavailable: %s", exc)
            return []
        return [m for m in markets
                if any(k in m.title.lower() for k in NFL_KEYWORDS)]

    def get_market(self, market_id: str) -> Market | None:
        try:
            mid = self._get(self.clob_host, f"/midpoint", params={"token_id": market_id})
            price = float(mid.get("mid", 0.0))
        except Exception:  # noqa: BLE001
            price = None
        book = self.get_orderbook(market_id)
        return Market(venue="polymarket", market_id=market_id, ticker=market_id,
                      title=market_id, yes_bid=book.best_bid, yes_ask=book.best_ask,
                      last_price=price)

    def get_orderbook(self, market_id: str, depth: int = 10) -> OrderBook:
        data = self._get(self.clob_host, "/book", params={"token_id": market_id})
        bids = [BookLevel(float(b["price"]), float(b["size"])) for b in (data.get("bids") or [])]
        asks = [BookLevel(float(a["price"]), float(a["size"])) for a in (data.get("asks") or [])]
        bids.sort(key=lambda l: -l.price)
        asks.sort(key=lambda l: l.price)
        return OrderBook(bids=bids[:depth], asks=asks[:depth], timestamp=time.time())

    # ---------------------------------------------------------------- trading
    def _client(self):
        if self._clob is not None:
            return self._clob
        if not self.credentials:
            raise RuntimeError(
                "Polymarket credentials are not configured. Set POLYMARKET_PRIVATE_KEY "
                "(and POLYMARKET_FUNDER for a proxy wallet)."
            )
        try:
            from py_clob_client.client import ClobClient
        except ImportError as exc:
            raise RuntimeError(
                "Polymarket trading needs the official client: pip install py-clob-client"
            ) from exc
        kwargs = {"key": self.credentials.private_key, "chain_id": POLYGON_CHAIN_ID}
        if self.credentials.funder:
            kwargs["funder"] = self.credentials.funder
            kwargs["signature_type"] = self.credentials.signature_type
        client = ClobClient(self.clob_host, **kwargs)
        client.set_api_creds(client.create_or_derive_api_creds())
        self._clob = client
        return client

    def place_order(self, order: Order) -> Order:
        from py_clob_client.clob_types import OrderArgs, OrderType as PmOrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        client = self._client()
        args = OrderArgs(
            token_id=order.market_id,
            price=round(float(order.price), 3),
            size=float(order.quantity),
            side=BUY if order.action == Action.BUY else SELL,
        )
        signed = client.create_order(args)
        kind = PmOrderType.GTC if order.order_type == OrderType.LIMIT else PmOrderType.FOK
        resp = client.post_order(signed, kind)
        order.status = str(resp.get("status", "submitted")) if isinstance(resp, dict) else "submitted"
        if isinstance(resp, dict):
            order.client_id = str(resp.get("orderID", order.client_id))
        return order

    def cancel_order(self, order_id: str) -> bool:
        client = self._client()
        client.cancel(order_id)
        return True

    def positions(self) -> list[Position]:
        # Positions live on-chain; the data API exposes them per wallet.
        if not self.credentials or not self.credentials.funder:
            return []
        try:
            data = self._get("https://data-api.polymarket.com", "/positions",
                             params={"user": self.credentials.funder})
        except Exception as exc:  # noqa: BLE001
            log.info("polymarket positions unavailable: %s", exc)
            return []
        out = []
        for p in (data if isinstance(data, list) else []):
            size = float(p.get("size", 0) or 0)
            if size == 0:
                continue
            out.append(Position(
                venue="polymarket", market_id=str(p.get("asset", "")),
                ticker=str(p.get("slug", "")), side=Side.YES, quantity=size,
                avg_price=float(p.get("avgPrice", 0) or 0),
                realised_pnl=float(p.get("realizedPnl", 0) or 0),
            ))
        return out

    def balance(self) -> float:
        try:
            client = self._client()
            bal = client.get_balance_allowance()
            return float(bal.get("balance", 0.0)) / 1e6  # USDC has 6 decimals
        except Exception as exc:  # noqa: BLE001
            log.info("polymarket balance unavailable: %s", exc)
            return 0.0
