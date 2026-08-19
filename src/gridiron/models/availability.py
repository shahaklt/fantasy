"""Weekly availability (injury) model.

Injuries are *persistent*: a hamstring costs three weeks, not three independent
coin flips. So availability is modelled as a two-state Markov chain per player,
parameterised by a stationary healthy-rate and a mean absence length. That
reproduces the fat left tail of season-long outcomes (the "my RB1 played 9
games" season) that an i.i.d. model completely misses.

Base rates were estimated from 2021-2025 nflverse weekly data restricted to
players who held a real role (>=60 opportunities in the season).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Stationary probability of being active in a given week, by position.
BASE_HEALTHY_RATE: dict[str, float] = {
    "QB": 0.900, "RB": 0.898, "WR": 0.916, "TE": 0.920, "K": 0.960, "DST": 1.0, "FB": 0.90,
}
#: Mean length (weeks) of an absence once it starts.
MEAN_ABSENCE_WEEKS: dict[str, float] = {
    "QB": 3.4, "RB": 2.9, "WR": 2.8, "TE": 2.7, "K": 2.0, "DST": 1.0, "FB": 2.8,
}
#: Age at which durability starts to decay, and the per-year penalty after it.
AGE_PIVOT: dict[str, float] = {"QB": 34.0, "RB": 27.0, "WR": 30.0, "TE": 31.0}
AGE_PENALTY_PER_YEAR = 0.012


@dataclass(frozen=True)
class AvailabilityModel:
    """Per-player two-state Markov availability chain.

    ``p_stay_out`` is the probability an absent player is still absent next week;
    ``p_get_hurt`` is calibrated so the chain's stationary healthy share equals
    ``healthy_rate``.
    """

    healthy_rate: np.ndarray   # (n_players,)
    p_get_hurt: np.ndarray     # (n_players,)
    p_stay_out: np.ndarray     # (n_players,)

    @property
    def expected_games(self) -> np.ndarray:
        return self.healthy_rate


def availability_params(
    positions: np.ndarray,
    ages: np.ndarray | None = None,
    games_missed_rate: np.ndarray | None = None,
    durability_prior_games: float = 24.0,
) -> AvailabilityModel:
    """Build the availability chain for a pool of players.

    Parameters
    ----------
    positions
        Position string per player.
    ages
        Age in years; older players at physical positions get a small penalty.
    games_missed_rate
        Observed share of team games missed over the player's recent history,
        blended toward the positional base rate with ``durability_prior_games``
        of prior weight.
    """
    n = len(positions)
    base = np.array([BASE_HEALTHY_RATE.get(str(p), 0.90) for p in positions], dtype=np.float64)
    mean_out = np.array([MEAN_ABSENCE_WEEKS.get(str(p), 2.9) for p in positions], dtype=np.float64)

    healthy = base.copy()
    if ages is not None:
        pivot = np.array([AGE_PIVOT.get(str(p), 30.0) for p in positions], dtype=np.float64)
        age = np.nan_to_num(np.asarray(ages, dtype=np.float64), nan=26.0)
        healthy = healthy - np.maximum(age - pivot, 0.0) * AGE_PENALTY_PER_YEAR

    if games_missed_rate is not None:
        obs = 1.0 - np.nan_to_num(np.asarray(games_missed_rate, dtype=np.float64), nan=np.nan)
        mask = np.isfinite(obs)
        # Empirical-Bayes blend of the player's own durability with his position's.
        w = np.zeros(n)
        w[mask] = 17.0 / (17.0 + durability_prior_games)
        healthy = np.where(mask, healthy * (1 - w) + np.clip(obs, 0.3, 1.0) * w, healthy)

    healthy = np.clip(healthy, 0.55, 0.995)
    p_stay_out = np.clip(1.0 - 1.0 / np.maximum(mean_out, 1.05), 0.05, 0.9)
    # Stationary distribution of a 2-state chain: healthy = q / (q + p_get_hurt)
    # where q = 1 - p_stay_out. Solve for p_get_hurt.
    q = 1.0 - p_stay_out
    p_get_hurt = np.clip(q * (1.0 - healthy) / np.maximum(healthy, 1e-6), 1e-4, 0.6)
    return AvailabilityModel(healthy_rate=healthy, p_get_hurt=p_get_hurt, p_stay_out=p_stay_out)


def simulate_availability(model: AvailabilityModel, n_sims: int, n_weeks: int,
                          rng: np.random.Generator) -> np.ndarray:
    """Simulate an (n_sims, n_weeks, n_players) boolean active mask.

    Memory-conscious callers should slice weeks; for a 12-team league pool of
    ~600 players and 10k sims this is ~100 MB as bool, so the simulator drives
    it week by week instead.
    """
    n = len(model.healthy_rate)
    out = np.empty((n_sims, n_weeks, n), dtype=bool)
    active = rng.random((n_sims, n)) < model.healthy_rate
    for w in range(n_weeks):
        u = rng.random((n_sims, n))
        stay_out = u < model.p_stay_out
        get_hurt = u < model.p_get_hurt
        active = np.where(active, ~get_hurt, ~stay_out)
        out[:, w, :] = active
    return out
