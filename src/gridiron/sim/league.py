"""Fantasy-league simulation: weekly lineups, matchups, playoff odds.

Built on top of the player-week simulation, so every team in your league is
evaluated against the *same* simulated NFL seasons. That matters: two managers
who both roster Bengals are correlated, and a league simulator that draws each
team independently will quietly misprice both their matchup odds and the value
of stacking.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import polars as pl

from ..scoring import SLOT_ELIGIBILITY, LeagueSettings


def _slot_order(slots: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(
        ((s, c) for s, c in slots.items() if c > 0 and s not in ("BN", "IR")),
        key=lambda kv: len(SLOT_ELIGIBILITY.get(kv[0], ())),
    )


def optimal_lineups(points: np.ndarray, positions: np.ndarray,
                    slots: dict[str, int]) -> tuple[np.ndarray, np.ndarray]:
    """Best legal lineup per simulation.

    Parameters
    ----------
    points
        ``(S, R)`` simulated points for the R players on one roster.
    positions
        ``(R,)`` position of each rostered player.

    Returns
    -------
    total, used
        ``(S,)`` starting-lineup points and ``(S, R)`` boolean starter mask.

    Slots are filled most-restrictive first, which is optimal for the nested
    eligibility fantasy lineups use (QB before SUPERFLEX, RB before FLEX).
    """
    S, R = points.shape
    used = np.zeros((S, R), dtype=bool)
    total = np.zeros(S, dtype=np.float64)
    for slot, count in _slot_order(slots):
        eligible = np.isin(positions, SLOT_ELIGIBILITY.get(slot, ()))
        if not eligible.any():
            continue
        for _ in range(count):
            masked = np.where(eligible[None, :] & ~used, points, -np.inf)
            best = masked.argmax(axis=1)
            gain = masked[np.arange(S), best]
            valid = np.isfinite(gain)
            total[valid] += gain[valid]
            used[np.arange(S)[valid], best[valid]] = True
    return total, used


@dataclass
class FantasyTeam:
    name: str
    player_ids: list[str]
    owner: str = ""


@dataclass
class LeagueSimResult:
    teams: list[str]
    weekly_points: np.ndarray     # (S, W, T)
    wins: np.ndarray              # (S, T)
    points_for: np.ndarray        # (S, T)
    playoff: np.ndarray           # (S, T) bool
    title: np.ndarray             # (S, T) bool
    seed: np.ndarray              # (S, T) final standing (1 = best)

    def summary(self) -> pl.DataFrame:
        return pl.DataFrame({
            "team": self.teams,
            "proj_wins": self.wins.mean(axis=0),
            "proj_points": self.points_for.mean(axis=0),
            "playoff_odds": self.playoff.mean(axis=0),
            "title_odds": self.title.mean(axis=0),
            "avg_finish": self.seed.mean(axis=0),
        }).sort("title_odds", descending=True)


class LeagueSimulator:
    """Simulate a whole fantasy season for a set of rosters."""

    def __init__(self, season_result, league: LeagueSettings):
        if season_result.weekly is None:
            raise ValueError("league simulation needs weekly points "
                             "(run simulate_season with keep_weekly=True)")
        self.res = season_result
        self.league = league
        self.index = {pid: i for i, pid in enumerate(season_result.player_ids)}
        self.positions = np.asarray(season_result.positions)

    def roster_weekly_points(self, player_ids: list[str]) -> np.ndarray:
        """(S, W) optimal starting-lineup points for one roster."""
        idx = [self.index[p] for p in player_ids if p in self.index]
        if not idx:
            W, S = self.res.weekly.shape[0], self.res.weekly.shape[1]
            return np.zeros((S, W))
        sub = self.res.weekly[:, :, idx]              # (W, S, R)
        pos = self.positions[idx]
        W, S, _ = sub.shape
        out = np.empty((S, W))
        for w in range(W):
            out[:, w], _ = optimal_lineups(sub[w], pos, self.league.starters)
        return out

    def simulate(self, teams: list[FantasyTeam], schedule: list[list[tuple[int, int]]] | None = None,
                 regular_weeks: int | None = None) -> LeagueSimResult:
        """Run the head-to-head season, playoffs included."""
        league = self.league
        regular_weeks = regular_weeks or league.regular_season_weeks
        n_teams = len(teams)
        weekly = np.stack([self.roster_weekly_points(t.player_ids) for t in teams], axis=2)  # (S,W,T)
        S, W, _ = weekly.shape
        regular_weeks = min(regular_weeks, W)

        schedule = schedule or round_robin(n_teams, regular_weeks)
        wins = np.zeros((S, n_teams))
        pf = weekly[:, :regular_weeks, :].sum(axis=1)
        for w, pairs in enumerate(schedule[:regular_weeks]):
            for a, b in pairs:
                a_win = weekly[:, w, a] > weekly[:, w, b]
                wins[:, a] += a_win
                wins[:, b] += ~a_win

        # Seeding: wins first, points for as the tiebreak.
        key = wins + pf / (pf.max() + 1.0) * 0.5
        order = np.argsort(-key, axis=1)
        seed = np.empty_like(order)
        rows = np.arange(S)[:, None]
        seed[rows, order] = np.arange(1, n_teams + 1)[None, :]

        n_playoff = min(league.playoff_teams, n_teams)
        playoff = seed <= n_playoff
        title = self._simulate_playoffs(weekly, seed, n_playoff, regular_weeks, W)

        return LeagueSimResult(
            teams=[t.name for t in teams], weekly_points=weekly, wins=wins,
            points_for=pf, playoff=playoff, title=title, seed=seed,
        )

    def _simulate_playoffs(self, weekly: np.ndarray, seed: np.ndarray, n_playoff: int,
                           regular_weeks: int, total_weeks: int) -> np.ndarray:
        """Single-elimination bracket on the weeks after the regular season."""
        S, _, T = weekly.shape
        alive = seed <= n_playoff
        rounds = int(np.ceil(np.log2(max(n_playoff, 2))))
        week = regular_weeks
        for _ in range(rounds):
            if week >= total_weeks:
                break
            pts = np.where(alive, weekly[:, week, :], -np.inf)
            # Pair the highest remaining seed against the lowest, per simulation.
            order = np.argsort(np.where(alive, seed, 10_000), axis=1)
            n_alive = alive.sum(axis=1)
            new_alive = np.zeros_like(alive)
            for s in range(S):
                live = order[s, : n_alive[s]]
                if len(live) <= 1:
                    new_alive[s, live] = True
                    continue
                # Byes for the top seeds when the bracket is not a power of two.
                n_games = len(live) // 2
                byes = live[: len(live) - 2 * n_games]
                new_alive[s, byes] = True
                playing = live[len(live) - 2 * n_games:]
                for g in range(n_games):
                    hi, lo = playing[g], playing[-1 - g]
                    winner = hi if pts[s, hi] >= pts[s, lo] else lo
                    new_alive[s, winner] = True
            alive = new_alive
            week += 1
        return alive & (alive.sum(axis=1, keepdims=True) == 1)


def round_robin(n_teams: int, weeks: int) -> list[list[tuple[int, int]]]:
    """Standard circle-method schedule, repeated to fill the season."""
    teams = list(range(n_teams))
    if n_teams % 2:
        teams.append(-1)
    n = len(teams)
    base = []
    for _ in range(n - 1):
        pairs = [(teams[i], teams[n - 1 - i]) for i in range(n // 2)
                 if teams[i] != -1 and teams[n - 1 - i] != -1]
        base.append(pairs)
        teams = [teams[0]] + [teams[-1]] + teams[1:-1]
    return [base[w % len(base)] for w in range(weeks)]


def start_sit(season_result, league: LeagueSettings, player_ids: list[str],
              week: int) -> pl.DataFrame:
    """Rank a roster for one week, with the probability each player should start."""
    index = {pid: i for i, pid in enumerate(season_result.player_ids)}
    idx = [index[p] for p in player_ids if p in index]
    if not idx:
        return pl.DataFrame()
    try:
        w = season_result.weeks.index(week)
    except ValueError:
        w = 0
    pts = season_result.weekly[w][:, idx]                 # (S, R)
    pos = np.asarray(season_result.positions)[idx]
    _, used = optimal_lineups(pts, pos, league.starters)
    return pl.DataFrame({
        "player_id": [season_result.player_ids[i] for i in idx],
        "player_name": [season_result.player_names[i] for i in idx],
        "position": list(pos),
        "team": [season_result.teams[i] for i in idx],
        "proj_points": pts.mean(axis=0),
        "floor": np.percentile(pts, 10, axis=0),
        "ceiling": np.percentile(pts, 90, axis=0),
        "start_pct": used.mean(axis=0),
    }).sort("start_pct", descending=True)
