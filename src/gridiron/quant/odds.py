"""Odds conversion and vig removal.

A quoted price is not a probability. Books and exchanges both quote a book that
sums to more than 100%, and the excess is not spread evenly: longshots carry
more of it than favourites (the favourite-longshot bias). Getting this step
wrong contaminates every downstream number, so all four standard devig methods
are implemented and the caller picks.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import brentq


# --------------------------------------------------------------------------------------
# Conversions
# --------------------------------------------------------------------------------------
def american_to_decimal(odds: float) -> float:
    odds = float(odds)
    return 1.0 + (odds / 100.0 if odds > 0 else 100.0 / abs(odds))


def decimal_to_american(dec: float) -> float:
    dec = float(dec)
    if dec <= 1.0:
        return float("-inf")
    return (dec - 1.0) * 100.0 if dec >= 2.0 else -100.0 / (dec - 1.0)


def american_to_prob(odds: float) -> float:
    return 1.0 / american_to_decimal(odds)


def prob_to_american(p: float) -> float:
    p = min(max(float(p), 1e-9), 1 - 1e-9)
    return decimal_to_american(1.0 / p)


def prob_to_decimal(p: float) -> float:
    return 1.0 / min(max(float(p), 1e-9), 1 - 1e-9)


def cents_to_prob(cents: float) -> float:
    """Kalshi-style contract price in cents -> probability."""
    return min(max(float(cents) / 100.0, 0.0), 1.0)


def prob_to_cents(p: float) -> float:
    return round(min(max(float(p), 0.0), 1.0) * 100.0, 2)


# --------------------------------------------------------------------------------------
# Devigging
# --------------------------------------------------------------------------------------
def devig_multiplicative(raw: np.ndarray) -> np.ndarray:
    """Scale every implied probability by the same factor. Simple; biased."""
    raw = np.asarray(raw, dtype=float)
    return raw / raw.sum()


def devig_additive(raw: np.ndarray) -> np.ndarray:
    """Subtract the overround equally from each outcome."""
    raw = np.asarray(raw, dtype=float)
    excess = (raw.sum() - 1.0) / len(raw)
    return np.clip(raw - excess, 1e-6, 1.0)


def devig_power(raw: np.ndarray) -> np.ndarray:
    """Raise probabilities to a common power k so they sum to one.

    Corrects the favourite-longshot bias more than multiplicative without the
    strong informed-trader assumption Shin makes. This is the pragmatic default
    for most books.
    """
    raw = np.clip(np.asarray(raw, dtype=float), 1e-9, 1 - 1e-9)

    def f(k: float) -> float:
        return float(np.sum(raw ** k) - 1.0)

    try:
        k = brentq(f, 0.2, 5.0, xtol=1e-10)
    except ValueError:
        return devig_multiplicative(raw)
    out = raw ** k
    return out / out.sum()


def devig_shin(raw: np.ndarray) -> np.ndarray:
    """Shin (1993): the margin arises from trading against insiders.

    Solves for the insider proportion z that makes the implied probabilities
    consistent, then backs out the fair prices. Tends to be the most accurate on
    high-margin retail markets, where the longshot distortion is largest.
    """
    raw = np.clip(np.asarray(raw, dtype=float), 1e-9, 1 - 1e-9)
    total = raw.sum()
    if total <= 1.0 + 1e-9:
        return raw / total

    def implied(z: float) -> np.ndarray:
        disc = np.sqrt(np.maximum(z ** 2 + 4.0 * (1.0 - z) * raw ** 2 / total, 0.0))
        return (disc - z) / (2.0 * (1.0 - z))

    def f(z: float) -> float:
        return float(implied(z).sum() - 1.0)

    try:
        z = brentq(f, 1e-8, 0.5, xtol=1e-12)
    except ValueError:
        return devig_power(raw)
    out = implied(z)
    return out / out.sum()


def devig_logarithmic(raw: np.ndarray) -> np.ndarray:
    """Odds-ratio (logarithmic) method: shift every outcome by a constant in log-odds."""
    raw = np.clip(np.asarray(raw, dtype=float), 1e-9, 1 - 1e-9)
    logit = np.log(raw / (1 - raw))

    def f(c: float) -> float:
        return float(np.sum(1.0 / (1.0 + np.exp(-(logit + c)))) - 1.0)

    try:
        c = brentq(f, -5.0, 5.0, xtol=1e-10)
    except ValueError:
        return devig_multiplicative(raw)
    out = 1.0 / (1.0 + np.exp(-(logit + c)))
    return out / out.sum()


DEVIG_METHODS = {
    "multiplicative": devig_multiplicative,
    "additive": devig_additive,
    "power": devig_power,
    "shin": devig_shin,
    "logarithmic": devig_logarithmic,
}


def devig(raw_probs, method: str = "power") -> np.ndarray:
    """Remove the book's margin from a set of raw implied probabilities."""
    fn = DEVIG_METHODS.get(method, devig_power)
    return fn(np.asarray(raw_probs, dtype=float))


def overround(raw_probs) -> float:
    """The book's margin, as a fraction (0.045 = a 4.5% hold)."""
    return float(np.sum(raw_probs) - 1.0)


# --------------------------------------------------------------------------------------
# Edge
# --------------------------------------------------------------------------------------
@dataclass
class Edge:
    """One side of one market, priced against a model."""

    model_prob: float
    market_prob: float
    price: float               # cost per $1 of payout (0-1), i.e. the traded price
    edge: float                # model_prob - market_prob
    ev_per_unit: float         # expected profit per unit staked
    fair_price: float
    roi: float

    def as_dict(self) -> dict:
        return {
            "model_prob": self.model_prob, "market_prob": self.market_prob,
            "price": self.price, "edge": self.edge, "ev_per_unit": self.ev_per_unit,
            "fair_price": self.fair_price, "roi": self.roi,
        }


def compute_edge(model_prob: float, price: float, fee: float = 0.0) -> Edge:
    """Edge for a binary contract bought at `price` (0-1) paying 1 if it resolves yes.

    `fee` is charged on profit, matching how prediction exchanges bill: Kalshi
    takes a cut of the winnings, not of the stake.
    """
    p = float(min(max(model_prob, 0.0), 1.0))
    price = float(min(max(price, 1e-6), 1 - 1e-6))
    payout = (1.0 - price) * (1.0 - fee)
    ev = p * payout - (1.0 - p) * price
    return Edge(
        model_prob=p,
        market_prob=price,
        price=price,
        edge=p - price,
        ev_per_unit=ev / price if price > 0 else 0.0,
        fair_price=p,
        roi=ev / price if price > 0 else 0.0,
    )
