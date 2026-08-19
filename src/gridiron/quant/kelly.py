"""Position sizing and portfolio risk.

Kelly maximises the long-run growth rate of a bankroll, and full Kelly is
famously too aggressive in practice: your edge estimate is itself uncertain, and
overbetting is punished far more harshly than underbetting. Everything here
therefore defaults to a fraction of Kelly, and the correlated form is used
whenever several positions resolve off the same game -- betting a quarterback
over and his receiver over is one bet, not two.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def kelly_fraction(prob: float, price: float, fee: float = 0.0) -> float:
    """Optimal bankroll fraction for a binary contract bought at `price`.

    For a contract costing `price` and paying 1: b = (1-price)/price is the odds
    received, and f* = (p*b - q)/b.
    """
    p = float(np.clip(prob, 0.0, 1.0))
    price = float(np.clip(price, 1e-6, 1 - 1e-6))
    b = (1.0 - price) * (1.0 - fee) / price
    if b <= 0:
        return 0.0
    f = (p * b - (1.0 - p)) / b
    return float(max(f, 0.0))


def fractional_kelly(prob: float, price: float, fraction: float = 0.25,
                     fee: float = 0.0, cap: float = 0.05) -> float:
    """Kelly scaled down and capped -- the size you should actually trade."""
    return float(min(kelly_fraction(prob, price, fee) * fraction, cap))


def kelly_with_uncertainty(prob: float, price: float, prob_sd: float,
                           fraction: float = 0.5, fee: float = 0.0,
                           cap: float = 0.05) -> float:
    """Shrink the edge by the uncertainty in the model's own probability.

    A 4-point edge you are confident in is not the same bet as a 4-point edge
    with a 6-point standard error. Shrinking by ``edge^2/(edge^2 + sd^2)``
    is the Bayesian posterior-mean adjustment when both are normal, and it kills
    the sizing on edges that are indistinguishable from noise.
    """
    edge = float(prob) - float(price)
    if edge <= 0:
        return 0.0
    shrink = edge ** 2 / (edge ** 2 + max(prob_sd, 1e-6) ** 2)
    adjusted = float(price) + edge * shrink
    return fractional_kelly(adjusted, price, fraction=fraction, fee=fee, cap=cap)


@dataclass
class PortfolioSizing:
    weights: np.ndarray
    expected_growth: float
    gross_exposure: float
    labels: list[str]

    def as_dict(self) -> dict:
        return {
            "positions": [
                {"label": l, "weight": float(w)} for l, w in zip(self.labels, self.weights)
            ],
            "expected_growth": self.expected_growth,
            "gross_exposure": self.gross_exposure,
        }


def correlated_kelly(probs: np.ndarray, prices: np.ndarray, corr: np.ndarray,
                     labels: list[str] | None = None, fraction: float = 0.25,
                     max_gross: float = 0.25, fee: float = 0.0) -> PortfolioSizing:
    """Approximate simultaneous Kelly for correlated binary positions.

    Uses the standard mean-variance approximation to log growth,
    ``f = Sigma^-1 mu``, where mu is each position's expected return per unit
    staked and Sigma its covariance. Exact for small edges, and small edges are
    the only ones that exist in a liquid market.
    """
    probs = np.asarray(probs, dtype=float)
    prices = np.clip(np.asarray(prices, dtype=float), 1e-6, 1 - 1e-6)
    n = len(probs)
    labels = labels or [f"pos{i}" for i in range(n)]

    payout = (1.0 - prices) * (1.0 - fee)
    mu = probs * payout - (1.0 - probs) * prices          # EV per unit staked
    # Variance of a binary payoff scaled to per-unit-staked returns.
    ret_win = payout / prices
    ret_lose = -1.0
    var = probs * (1 - probs) * (ret_win - ret_lose) ** 2
    sd = np.sqrt(np.maximum(var, 1e-12))
    mu_ret = mu / prices

    corr = np.asarray(corr, dtype=float)
    if corr.shape != (n, n):
        corr = np.eye(n)
    cov = corr * np.outer(sd, sd)
    cov = cov + np.eye(n) * 1e-6

    try:
        raw = np.linalg.solve(cov, mu_ret)
    except np.linalg.LinAlgError:
        raw = mu_ret / np.maximum(np.diag(cov), 1e-9)

    weights = np.maximum(raw, 0.0) * fraction
    gross = weights.sum()
    if gross > max_gross and gross > 0:
        weights = weights * (max_gross / gross)
    growth = float(weights @ mu_ret - 0.5 * weights @ cov @ weights)
    return PortfolioSizing(weights=weights, expected_growth=growth,
                           gross_exposure=float(weights.sum()), labels=labels)


# --------------------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------------------
def simulate_bankroll(edges: np.ndarray, prices: np.ndarray, weights: np.ndarray,
                      n_sims: int = 20_000, rounds: int = 1,
                      seed: int | None = None) -> np.ndarray:
    """Simulate terminal bankroll multiples for a set of independent positions."""
    rng = np.random.default_rng(seed)
    probs = np.clip(np.asarray(edges, dtype=float), 0.0, 1.0)
    prices = np.clip(np.asarray(prices, dtype=float), 1e-6, 1 - 1e-6)
    weights = np.asarray(weights, dtype=float)
    bank = np.ones(n_sims)
    for _ in range(rounds):
        outcomes = rng.random((n_sims, len(probs))) < probs
        ret = np.where(outcomes, (1.0 - prices) / prices, -1.0)
        bank = bank * (1.0 + ret @ weights)
        bank = np.maximum(bank, 0.0)
    return bank


def risk_metrics(bankroll: np.ndarray, alpha: float = 0.05) -> dict:
    """VaR, CVaR, ruin probability and growth statistics for a bankroll sample."""
    b = np.asarray(bankroll, dtype=float)
    losses = 1.0 - b
    var = float(np.percentile(losses, 100 * (1 - alpha)))
    tail = losses[losses >= var]
    return {
        "mean_return": float(b.mean() - 1.0),
        "median_return": float(np.median(b) - 1.0),
        "volatility": float(b.std()),
        f"var_{int(alpha*100)}": var,
        f"cvar_{int(alpha*100)}": float(tail.mean()) if len(tail) else var,
        "prob_loss": float((b < 1.0).mean()),
        "prob_ruin_50": float((b < 0.5).mean()),
        "expected_log_growth": float(np.mean(np.log(np.maximum(b, 1e-9)))),
    }
