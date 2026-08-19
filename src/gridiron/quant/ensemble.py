"""Combining a model forecast with the market price.

A liquid prediction market is a strong forecaster. The right posture is not
"my model versus the market" but "how much does my model move me off the
market" -- so forecasts are combined in log-odds space, where the market acts
as the prior and the model contributes a bounded number of points of evidence.

Logarithmic (log-odds) pooling is used rather than a plain average because it
is externally Bayesian: pooling then updating gives the same answer as updating
then pooling, which a linear average does not.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import minimize_scalar

from .calibration import log_loss


def logit(p):
    p = np.clip(np.asarray(p, dtype=float), 1e-9, 1 - 1e-9)
    return np.log(p / (1 - p))


def expit(x):
    return 1.0 / (1.0 + np.exp(-np.asarray(x, dtype=float)))


def log_odds_pool(probs: np.ndarray, weights: np.ndarray | None = None) -> np.ndarray:
    """Weighted geometric pooling of probabilities in log-odds space."""
    probs = np.atleast_2d(np.asarray(probs, dtype=float))
    n = probs.shape[0]
    w = np.ones(n) / n if weights is None else np.asarray(weights, dtype=float)
    w = w / w.sum()
    return expit(np.tensordot(w, logit(probs), axes=(0, 0)))


def blend_with_market(model_prob, market_prob, model_weight: float = 0.35,
                      max_shift: float = 1.2) -> np.ndarray:
    """Nudge the market price toward the model, with a hard cap on the move.

    `max_shift` bounds the deviation in log-odds (1.2 is roughly a factor of
    3.3 in odds). The cap matters: it stops one badly-specified projection from
    producing an enormous position against a market that knows something the
    model does not -- an injury, a weather call, a benching.
    """
    lm = logit(model_prob)
    lk = logit(market_prob)
    shift = np.clip((lm - lk) * model_weight, -max_shift, max_shift)
    return expit(lk + shift)


def fit_model_weight(model_probs: np.ndarray, market_probs: np.ndarray,
                     outcomes: np.ndarray) -> float:
    """Choose the blend weight that minimises historical log loss."""
    def loss(w: float) -> float:
        return log_loss(blend_with_market(model_probs, market_probs, float(w)), outcomes)

    res = minimize_scalar(loss, bounds=(0.0, 1.0), method="bounded")
    return float(np.clip(res.x, 0.0, 1.0))


def disagreement(model_prob, market_prob) -> np.ndarray:
    """How far apart the two forecasts are, in log-odds ("points of evidence")."""
    return logit(model_prob) - logit(market_prob)


def ensemble_report(model_prob: float, market_prob: float, model_weight: float = 0.35,
                    model_sd: float = 0.0) -> dict:
    """One market's blended view, with the size of the disagreement made explicit."""
    blended = float(blend_with_market(model_prob, market_prob, model_weight))
    return {
        "model_prob": float(model_prob),
        "market_prob": float(market_prob),
        "blended_prob": blended,
        "model_weight": model_weight,
        "disagreement_logodds": float(disagreement(model_prob, market_prob)),
        "edge_raw": float(model_prob) - float(market_prob),
        "edge_blended": blended - float(market_prob),
        "model_sd": model_sd,
    }
