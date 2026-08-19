"""Order-book microstructure signals for prediction markets.

These are the standard short-horizon signals from equity/crypto market making,
applied to binary contracts:

* **Micro-price** (Stoikov) -- the mid adjusted for book imbalance. It is a
  martingale by construction and predicts the next mid better than the mid does.
* **Order-flow imbalance** -- signed change in resting depth at the touch,
  which has a near-linear relationship with short-horizon price change.
* **Book imbalance, spread, depth** -- the state variables that make an
  incoming order move the price a lot or a little.
* **Realised volatility and trade-sign autocorrelation** -- how much of the
  recent move is genuine repricing versus noise around a stable level.

For a game contract these are the mechanics behind "sentiment is shifting":
sentiment shows up as a persistent order-flow imbalance before it shows up in
the last-traded price.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class BookLevel:
    price: float   # probability units, 0-1
    size: float


@dataclass
class OrderBook:
    """Top-of-book snapshot for one binary contract."""

    bids: list[BookLevel] = field(default_factory=list)   # descending price
    asks: list[BookLevel] = field(default_factory=list)   # ascending price
    timestamp: float = 0.0

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return self.best_bid if self.best_ask is None else self.best_ask
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def depth(self, levels: int = 5) -> tuple[float, float]:
        bid = sum(l.size for l in self.bids[:levels])
        ask = sum(l.size for l in self.asks[:levels])
        return bid, ask

    def imbalance(self, levels: int = 5) -> float:
        """(bid - ask) / (bid + ask) depth. Positive means buying pressure."""
        b, a = self.depth(levels)
        total = b + a
        return float((b - a) / total) if total > 0 else 0.0

    def micro_price(self, levels: int = 1) -> float | None:
        """Size-weighted mid: the touch price the resting book actually implies."""
        if not self.bids or not self.asks:
            return self.mid
        b, a = self.depth(levels)
        if b + a <= 0:
            return self.mid
        # Heavier bid depth pulls the fair price toward the ask, and vice versa.
        return float((self.best_bid * a + self.best_ask * b) / (a + b))

    def weighted_mid(self, levels: int = 5) -> float | None:
        if self.mid is None:
            return None
        return float(self.mid + 0.5 * (self.spread or 0.0) * self.imbalance(levels))

    def sweep_cost(self, size: float, side: str = "buy") -> float | None:
        """Average fill price to take `size` contracts off the book."""
        book = self.asks if side == "buy" else self.bids
        if not book:
            return None
        remaining, cost = float(size), 0.0
        for level in book:
            take = min(remaining, level.size)
            cost += take * level.price
            remaining -= take
            if remaining <= 0:
                break
        filled = size - max(remaining, 0.0)
        return float(cost / filled) if filled > 0 else None


def order_flow_imbalance(prev: OrderBook, curr: OrderBook) -> float:
    """Signed change in depth at the touch (Cont-Kukanov-Stoikov OFI).

    Depth added at the bid or removed from the ask is buying pressure; the
    reverse is selling pressure. It is the increment that actually moves price,
    which is why it beats raw volume as a short-horizon predictor.
    """
    def touch(book: OrderBook):
        bid = book.bids[0] if book.bids else BookLevel(0.0, 0.0)
        ask = book.asks[0] if book.asks else BookLevel(1.0, 0.0)
        return bid, ask

    pb, pa = touch(prev)
    cb, ca = touch(curr)

    if cb.price > pb.price:
        e_bid = cb.size
    elif cb.price == pb.price:
        e_bid = cb.size - pb.size
    else:
        e_bid = -pb.size

    if ca.price < pa.price:
        e_ask = ca.size
    elif ca.price == pa.price:
        e_ask = ca.size - pa.size
    else:
        e_ask = -pa.size

    return float(e_bid - e_ask)


def realised_volatility(prices: np.ndarray, annualise: bool = False,
                        periods_per_year: float = 252 * 6.5 * 60) -> float:
    """Standard deviation of successive price changes (in probability units)."""
    p = np.asarray(prices, dtype=float)
    if len(p) < 3:
        return 0.0
    d = np.diff(p)
    vol = float(np.std(d))
    return vol * np.sqrt(periods_per_year) if annualise else vol


def price_momentum(prices: np.ndarray, window: int = 20) -> float:
    """Recent drift, normalised by its own volatility (a z-score, not a return)."""
    p = np.asarray(prices, dtype=float)
    if len(p) < max(window, 3):
        return 0.0
    recent = p[-window:]
    drift = recent[-1] - recent[0]
    vol = np.std(np.diff(recent))
    return float(drift / (vol * np.sqrt(len(recent)))) if vol > 0 else 0.0


def mean_reversion_score(prices: np.ndarray, window: int = 40) -> float:
    """Lag-1 autocorrelation of returns. Negative implies mean reversion."""
    p = np.asarray(prices, dtype=float)[-window:]
    if len(p) < 5:
        return 0.0
    d = np.diff(p)
    if np.std(d) == 0:
        return 0.0
    return float(np.corrcoef(d[:-1], d[1:])[0, 1]) if len(d) > 2 else 0.0


def lead_lag(a: np.ndarray, b: np.ndarray, max_lag: int = 10) -> dict:
    """Which of two venues moves first.

    Returns the lag maximising cross-correlation of returns; a positive lag
    means series `a` leads `b`, i.e. `a` is where price discovery happens.
    """
    ra = np.diff(np.asarray(a, dtype=float))
    rb = np.diff(np.asarray(b, dtype=float))
    n = min(len(ra), len(rb))
    if n < max_lag + 3:
        return {"lag": 0, "correlation": 0.0}
    ra, rb = ra[-n:], rb[-n:]
    best_lag, best_corr = 0, 0.0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            x, y = ra[: n - lag], rb[lag:]
        else:
            x, y = ra[-lag:], rb[: n + lag]
        if len(x) < 3 or np.std(x) == 0 or np.std(y) == 0:
            continue
        c = float(np.corrcoef(x, y)[0, 1])
        if abs(c) > abs(best_corr):
            best_lag, best_corr = lag, c
    return {"lag": best_lag, "correlation": best_corr}


def book_features(book: OrderBook, prev: OrderBook | None = None,
                  price_history: np.ndarray | None = None) -> dict:
    """Everything the sentiment panel shows for one contract, in one call."""
    bid_depth, ask_depth = book.depth()
    feat = {
        "best_bid": book.best_bid,
        "best_ask": book.best_ask,
        "mid": book.mid,
        "micro_price": book.micro_price(),
        "weighted_mid": book.weighted_mid(),
        "spread": book.spread,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "imbalance": book.imbalance(),
        "ofi": order_flow_imbalance(prev, book) if prev is not None else 0.0,
    }
    if price_history is not None and len(price_history) > 2:
        feat.update({
            "realised_vol": realised_volatility(price_history),
            "momentum": price_momentum(price_history),
            "mean_reversion": mean_reversion_score(price_history),
        })
    return feat
