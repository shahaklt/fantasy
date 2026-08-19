"""Synthetic venue for testing the live tape without credentials or network.

Generates a plausible order book for each game: a latent fair value that random
walks, a spread that widens with uncertainty, and depth that leans one way when
the fair value has moved but the quotes have not caught up yet. That last part
is the point -- it produces genuine order-flow imbalance, so the microstructure
signals in the sentiment panel can be verified end to end.

Enable with ``GRIDIRON_DEMO_VENUE=1``.
"""
from __future__ import annotations

import os
import time

import numpy as np

from ..quant.microstructure import BookLevel, OrderBook
from .base import Action, Market, Order, Position, Side


class DemoVenue:
    """Deterministic-per-seed synthetic exchange."""

    venue = "demo"
    authenticated = True

    def __init__(self, markets: list[tuple[str, str, float]] | None = None,
                 seed: int = 7, volatility: float = 0.004):
        self.rng = np.random.default_rng(seed)
        self.volatility = volatility
        self._start = time.time()
        specs = markets or [
            ("DEMO-KC-BUF", "Chiefs beat Bills", 0.57),
            ("DEMO-PHI-DAL", "Eagles beat Cowboys", 0.63),
            ("DEMO-DET-GB", "Lions beat Packers", 0.55),
            ("DEMO-SF-LA", "49ers beat Rams", 0.48),
            ("DEMO-CIN-BAL", "Bengals beat Ravens", 0.44),
            ("DEMO-OVER-KCBUF", "Chiefs/Bills total points over 48.5", 0.51),
            ("DEMO-SPREAD-PHIDAL", "Eagles cover -3.5 vs Cowboys", 0.52),
            ("DEMO-TT-DET", "Lions team total over 27.5", 0.49),
        ]
        self._fair = {m[0]: m[2] for m in specs}
        self._specs = {m[0]: m for m in specs}
        self._last_step = time.time()

    # ------------------------------------------------------------------ state
    def _advance(self) -> None:
        """Random-walk each fair value, with mean reversion toward its anchor."""
        now = time.time()
        dt = max(now - self._last_step, 0.0)
        if dt < 0.25:
            return
        self._last_step = now
        steps = max(int(dt / 0.5), 1)
        for mid, (_, _, anchor) in self._specs.items():
            f = self._fair[mid]
            for _ in range(steps):
                shock = self.rng.normal(0, self.volatility)
                f += shock + 0.02 * (anchor - f)
            self._fair[mid] = float(np.clip(f, 0.03, 0.97))

    def _quote(self, market_id: str) -> tuple[float, float]:
        fair = self._fair[market_id]
        # Wider spread near the extremes, where a tick is worth more.
        half = 0.005 + 0.01 * (1 - abs(fair - 0.5) * 2) * 0.5
        return round(fair - half, 3), round(fair + half, 3)

    # ------------------------------------------------------------ market data
    def list_markets(self, query: str | None = None, limit: int = 200) -> list[Market]:
        self._advance()
        out = []
        for mid, (_, title, _) in self._specs.items():
            bid, ask = self._quote(mid)
            out.append(Market(venue=self.venue, market_id=mid, ticker=mid, title=title,
                              yes_bid=bid, yes_ask=ask, last_price=self._fair[mid],
                              volume=float(self.rng.integers(500, 50_000)),
                              open_interest=float(self.rng.integers(1_000, 90_000))))
        if query:
            q = query.lower()
            out = [m for m in out if q in m.title.lower() or q in m.ticker.lower()]
        return out[:limit]

    def list_nfl_markets(self, limit: int = 200) -> list[Market]:
        return self.list_markets(limit=limit)

    def get_market(self, market_id: str) -> Market | None:
        found = [m for m in self.list_markets() if m.market_id == market_id]
        return found[0] if found else None

    def get_orderbook(self, market_id: str, depth: int = 10) -> OrderBook:
        self._advance()
        if market_id not in self._fair:
            return OrderBook()
        bid, ask = self._quote(market_id)
        # Depth leans toward the side the fair value is drifting away from,
        # which is what makes order-flow imbalance informative.
        lean = float(np.clip(self.rng.normal(0, 0.35), -0.9, 0.9))
        bids, asks = [], []
        for i in range(depth):
            size_b = max(int(self.rng.gamma(2.0, 220) * (1 + lean)), 1)
            size_a = max(int(self.rng.gamma(2.0, 220) * (1 - lean)), 1)
            bids.append(BookLevel(round(max(bid - i * 0.01, 0.01), 3), size_b))
            asks.append(BookLevel(round(min(ask + i * 0.01, 0.99), 3), size_a))
        return OrderBook(bids=bids, asks=asks, timestamp=time.time())

    # ---------------------------------------------------------------- trading
    def place_order(self, order: Order) -> Order:
        order.status = "filled"
        order.filled = order.quantity
        order.avg_fill_price = order.price
        return order

    def cancel_order(self, order_id: str) -> bool:
        return True

    def positions(self) -> list[Position]:
        return []

    def balance(self) -> float:
        return 1000.0


def demo_enabled() -> bool:
    return os.environ.get("GRIDIRON_DEMO_VENUE", "").strip().lower() in ("1", "true", "yes")
