"""Probability a player is still on the board at a given pick.

Draft position is modelled as a latent normal around the market's consensus
rank, with the dispersion the market itself reports (the spread between expert
best/worst ranks). That yields P(taken before pick k) in closed form, which is
enough for a fast board, while the full draft simulator handles the effects a
closed form cannot -- positional runs, roster needs and opponent tendencies.
"""
from __future__ import annotations

import numpy as np
import polars as pl


def _norm_cdf(x: np.ndarray) -> np.ndarray:
    from math import sqrt
    from scipy.special import erf  # type: ignore

    return 0.5 * (1.0 + erf(np.asarray(x) / sqrt(2.0)))


def survival_probability(adp: np.ndarray, adp_sd: np.ndarray, pick: int,
                         noise_floor: float = 3.0) -> np.ndarray:
    """P(player is still available at overall pick number `pick`).

    A player whose consensus ADP is exactly `pick` is a coin flip; the spread
    of expert opinion sets how quickly that probability decays either side.
    """
    sd = np.maximum(np.nan_to_num(adp_sd, nan=12.0), noise_floor)
    adp = np.nan_to_num(adp, nan=400.0)
    # P(draft position >= pick) = 1 - Phi((pick - adp)/sd)
    return np.clip(1.0 - _norm_cdf((pick - adp) / sd), 0.0, 1.0)


def snake_picks(draft_slot: int, teams: int, rounds: int) -> list[int]:
    """Overall pick numbers for `draft_slot` in a snake draft (1-indexed)."""
    picks = []
    for rnd in range(1, rounds + 1):
        if rnd % 2 == 1:
            picks.append((rnd - 1) * teams + draft_slot)
        else:
            picks.append((rnd - 1) * teams + (teams - draft_slot + 1))
    return picks


def linear_picks(draft_slot: int, teams: int, rounds: int) -> list[int]:
    return [(rnd - 1) * teams + draft_slot for rnd in range(1, rounds + 1)]


def board_with_availability(board: pl.DataFrame, picks: list[int]) -> pl.DataFrame:
    """Attach P(available) at each of *your* upcoming picks."""
    adp = board["adp"].to_numpy() if "adp" in board.columns else np.full(board.height, 400.0)
    sd = board["adp_sd"].to_numpy() if "adp_sd" in board.columns else np.full(board.height, 12.0)
    cols = []
    for p in picks[:6]:
        cols.append(pl.Series(f"avail_at_{p}", survival_probability(adp, sd, p)))
    return board.with_columns(cols) if cols else board
