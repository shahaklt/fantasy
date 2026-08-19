"""Analytic game model: Stern's Brownian motion framework.

Stern (1994) showed that an NFL final margin is well described as normal around
the point spread, and that in-game the score difference behaves like Brownian
motion with drift. That single idea prices almost everything a game market
offers -- moneyline, spread, total, team totals, and their live equivalents --
in closed form, which is what makes it useful as a fast complement to the Monte
Carlo engine and as a sanity check on it.

Constants are the same ones fitted from nflverse schedules 1999-2025:
margin sd 12.73 around the closing spread, total sd 13.19 around the total.
Stern's original NFL figure was 13.86; the tighter modern number reflects
sharper closing lines.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import norm

DEFAULT_MARGIN_SD = 12.73
DEFAULT_TOTAL_SD = 13.19


@dataclass
class GameLine:
    """A game as the market prices it."""

    home_team: str
    away_team: str
    spread: float          # home margin the market expects (positive = home favoured)
    total: float
    margin_sd: float = DEFAULT_MARGIN_SD
    total_sd: float = DEFAULT_TOTAL_SD

    @property
    def home_implied(self) -> float:
        return (self.total + self.spread) / 2.0

    @property
    def away_implied(self) -> float:
        return (self.total - self.spread) / 2.0


def win_probability(spread: float, margin_sd: float = DEFAULT_MARGIN_SD,
                    push_at_zero: bool = True) -> float:
    """P(home wins) for a given spread.

    NFL margins cluster hard on 3 and 7, so the continuous normal slightly
    misprices those keys; the error is small (~1pp) and is corrected by the
    Monte Carlo engine, which samples real scoring events.
    """
    if not push_at_zero:
        return float(np.clip(norm.cdf(spread / margin_sd), 0.0, 1.0))
    # Scores are integers, so treat the continuous margin as a discrete one with
    # a continuity correction and split the tie mass. Doing it this way keeps
    # the result symmetric -- an earlier version subtracted the tie mass from
    # the home side only, which biased every favourite downward and broke the
    # inversion in `implied_volatility`.
    z_hi = (0.5 - spread) / margin_sd
    z_lo = (-0.5 - spread) / margin_sd
    p_win = float(norm.sf(z_hi))
    p_tie = float(norm.cdf(z_hi) - norm.cdf(z_lo))
    return float(np.clip(p_win + 0.5 * p_tie, 0.0, 1.0))


def cover_probability(spread: float, line: float,
                      margin_sd: float = DEFAULT_MARGIN_SD) -> float:
    """P(home covers `line`), where `line` is the handicap laid on the home side."""
    return float(norm.sf((line - spread) / margin_sd))


def total_over_probability(total: float, line: float,
                           total_sd: float = DEFAULT_TOTAL_SD) -> float:
    return float(norm.sf((line - total) / total_sd))


def team_total_over_probability(team_implied: float, line: float,
                                team_sd: float | None = None) -> float:
    """A single team's points are roughly half the game's variance plus the margin's."""
    if team_sd is None:
        team_sd = float(np.sqrt(DEFAULT_TOTAL_SD ** 2 + DEFAULT_MARGIN_SD ** 2) / 2.0)
    return float(norm.sf((line - team_implied) / team_sd))


def implied_spread_from_moneyline(prob: float,
                                  margin_sd: float = DEFAULT_MARGIN_SD) -> float:
    """Invert a fair moneyline probability back into a point spread."""
    p = float(np.clip(prob, 1e-6, 1 - 1e-6))
    return float(norm.ppf(p) * margin_sd)


def implied_volatility(spread: float, win_prob: float) -> float:
    """The margin sd the market's moneyline and spread jointly imply.

    Reading the two quotes together backs out how *uncertain* the market thinks
    the game is -- the sports analogue of an option's implied vol. A number well
    above the 12.7 baseline means the market is pricing unusual variance
    (a backup quarterback, weather, a team with nothing to play for).
    """
    p = float(np.clip(win_prob, 1e-6, 1 - 1e-6))
    z = norm.ppf(p)
    if abs(z) < 1e-8:
        return float("nan")
    return float(abs(spread / z))


def live_win_probability(lead: float, seconds_remaining: float, spread: float = 0.0,
                         game_seconds: float = 3600.0,
                         margin_sd: float = DEFAULT_MARGIN_SD) -> float:
    """In-game win probability via a Brownian bridge.

    With a fraction ``t`` of the game elapsed, the remaining margin is normal
    with mean ``(1-t) * spread`` and sd ``margin_sd * sqrt(1-t)``. Stern's
    result; still the cleanest live model that needs no play-by-play features.
    """
    frac_left = float(np.clip(seconds_remaining / game_seconds, 0.0, 1.0))
    if frac_left <= 1e-6:
        return 1.0 if lead > 0 else (0.0 if lead < 0 else 0.5)
    mean = lead + spread * frac_left
    sd = margin_sd * np.sqrt(frac_left)
    return float(norm.cdf(mean / sd))


def price_game(line: GameLine, alt_spreads: list[float] | None = None,
               alt_totals: list[float] | None = None) -> dict:
    """Full analytic price sheet for one game."""
    alt_spreads = alt_spreads or [line.spread]
    alt_totals = alt_totals or [line.total]
    home_wp = win_probability(line.spread, line.margin_sd)
    return {
        "home_team": line.home_team,
        "away_team": line.away_team,
        "spread": line.spread,
        "total": line.total,
        "home_win_prob": home_wp,
        "away_win_prob": 1.0 - home_wp,
        "home_implied_points": line.home_implied,
        "away_implied_points": line.away_implied,
        "implied_volatility": implied_volatility(line.spread, home_wp),
        "spreads": [
            {"line": s, "home_cover": cover_probability(line.spread, s, line.margin_sd)}
            for s in alt_spreads
        ],
        "totals": [
            {"line": t, "over": total_over_probability(line.total, t, line.total_sd)}
            for t in alt_totals
        ],
    }
