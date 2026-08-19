"""Forecast scoring, calibration and closing-line value.

A model that is 60% confident should be right 60% of the time. Accuracy alone
never reveals miscalibration, so this module tracks proper scoring rules, a
reliability curve, and a recalibration map you can apply to future forecasts.

For anyone trading these markets, closing-line value is the honest scoreboard:
beating the closing price consistently is the only short-run evidence of edge
that is not mostly luck.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def brier_score(probs: np.ndarray, outcomes: np.ndarray) -> float:
    p = np.asarray(probs, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    return float(np.mean((p - y) ** 2))


def log_loss(probs: np.ndarray, outcomes: np.ndarray, eps: float = 1e-12) -> float:
    p = np.clip(np.asarray(probs, dtype=float), eps, 1 - eps)
    y = np.asarray(outcomes, dtype=float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier_skill_score(probs: np.ndarray, outcomes: np.ndarray) -> float:
    """Improvement over always predicting the base rate. Above 0 is useful."""
    y = np.asarray(outcomes, dtype=float)
    base = float(y.mean())
    ref = np.mean((base - y) ** 2)
    return float(1.0 - brier_score(probs, y) / ref) if ref > 0 else 0.0


def reliability_curve(probs: np.ndarray, outcomes: np.ndarray, bins: int = 10) -> dict:
    """Observed frequency per predicted-probability bucket."""
    p = np.asarray(probs, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    edges = np.linspace(0.0, 1.0, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    rows = []
    for b in range(bins):
        sel = idx == b
        if not sel.any():
            continue
        rows.append({
            "bin_low": float(edges[b]), "bin_high": float(edges[b + 1]),
            "predicted": float(p[sel].mean()), "observed": float(y[sel].mean()),
            "count": int(sel.sum()),
        })
    ece = sum(r["count"] * abs(r["predicted"] - r["observed"]) for r in rows) / max(len(p), 1)
    return {"bins": rows, "expected_calibration_error": float(ece)}


@dataclass
class IsotonicCalibrator:
    """Monotone recalibration map fitted on historical forecasts."""

    x: np.ndarray
    y: np.ndarray

    def __call__(self, probs) -> np.ndarray:
        return np.interp(np.asarray(probs, dtype=float), self.x, self.y)

    @classmethod
    def fit(cls, probs: np.ndarray, outcomes: np.ndarray) -> "IsotonicCalibrator":
        p = np.asarray(probs, dtype=float)
        y = np.asarray(outcomes, dtype=float)
        order = np.argsort(p)
        ps, ys = p[order], y[order]
        # Pool adjacent violators for a non-decreasing fit.
        vals, wts = [], []
        for v in ys:
            vals.append(float(v))
            wts.append(1.0)
            while len(vals) > 1 and vals[-2] > vals[-1]:
                v2, w2 = vals.pop(), wts.pop()
                v1, w1 = vals.pop(), wts.pop()
                vals.append((v1 * w1 + v2 * w2) / (w1 + w2))
                wts.append(w1 + w2)
        fitted = np.repeat(vals, [int(w) for w in wts])[: len(ys)]
        return cls(x=ps, y=np.clip(fitted, 0.0, 1.0))


def platt_scale(probs: np.ndarray, outcomes: np.ndarray, iters: int = 200,
                lr: float = 0.1) -> tuple[float, float]:
    """Fit logistic a,b so that sigmoid(a*logit(p)+b) is calibrated."""
    p = np.clip(np.asarray(probs, dtype=float), 1e-6, 1 - 1e-6)
    y = np.asarray(outcomes, dtype=float)
    z = np.log(p / (1 - p))
    a, b = 1.0, 0.0
    for _ in range(iters):
        q = 1.0 / (1.0 + np.exp(-(a * z + b)))
        ga = np.mean((q - y) * z)
        gb = np.mean(q - y)
        a -= lr * ga
        b -= lr * gb
    return float(a), float(b)


def closing_line_value(entry_price: np.ndarray, closing_price: np.ndarray,
                       side: np.ndarray | None = None) -> dict:
    """Did you get a better price than the close?

    `side` is +1 for a yes/long position and -1 for a no/short. Positive CLV
    means you consistently bought below where the market settled, which is the
    strongest available evidence that the edge is real rather than lucky.
    """
    entry = np.asarray(entry_price, dtype=float)
    close = np.asarray(closing_price, dtype=float)
    side = np.ones_like(entry) if side is None else np.asarray(side, dtype=float)
    clv = side * (close - entry)
    return {
        "mean_clv": float(clv.mean()),
        "median_clv": float(np.median(clv)),
        "pct_positive": float((clv > 0).mean()),
        "n": int(len(clv)),
        "clv_per_trade_bps": float(clv.mean() * 10_000),
    }
