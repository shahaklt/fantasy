"""Compare simulated positional finish curves against actual history.

The right comparison is rank-for-rank *within a simulation*: the player who
finishes WR3 in a given season is not known in advance, so an ex-ante mean
projection can never be compared directly to an ex-post leaderboard. Sorting
each simulated season and averaging across simulations produces the ex-ante
distribution of the rank-k finisher, which is directly comparable to history.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np
import polars as pl

from gridiron.data import nflverse as nv
from gridiron.models.projections import build_projections
from gridiron.scoring import LeagueSettings, Scoring, score_expression
from gridiron.sim import MonteCarloEngine, build_sim_inputs

RANKS = (1, 3, 6, 12, 24, 36)


def historical_curve(seasons: list[int], scoring: Scoring) -> dict[str, dict[int, float]]:
    ps = nv.load_player_stats(seasons).filter(pl.col("season_type") == "REG")
    ps = ps.filter(pl.col("position").is_in(["QB", "RB", "WR", "TE"]))
    ps = ps.with_columns(score_expression(scoring))
    tot = ps.group_by(["season", "player_id", "position"]).agg(
        pl.col("fantasy_points").sum().alias("pts"))
    out: dict[str, dict[int, float]] = {}
    for pos in ("QB", "RB", "WR", "TE"):
        out[pos] = {}
        vals = {r: [] for r in RANKS}
        for season in seasons:
            s = tot.filter((pl.col("position") == pos) & (pl.col("season") == season))
            arr = np.sort(s["pts"].to_numpy())[::-1]
            for r in RANKS:
                if len(arr) >= r:
                    vals[r].append(arr[r - 1])
        for r in RANKS:
            out[pos][r] = float(np.mean(vals[r])) if vals[r] else float("nan")
    return out


def simulated_curve(res, ranks=RANKS) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = {}
    pos_arr = np.asarray(res.positions)
    for pos in ("QB", "RB", "WR", "TE"):
        sel = pos_arr == pos
        if sel.sum() == 0:
            continue
        sub = np.sort(res.totals[:, sel], axis=1)[:, ::-1]
        out[pos] = {r: float(sub[:, r - 1].mean()) for r in ranks if sub.shape[1] >= r}
    return out


def main(n_sims: int = 20_000, season: int = 2026, seasons_hist=(2023, 2024, 2025)):
    league = LeagueSettings()
    proj = build_projections(season, league)
    inputs = build_sim_inputs(proj)
    engine = MonteCarloEngine(inputs, league.scoring, seed=2026)
    res = engine.simulate_season(n_sims=n_sims, keep_weekly=False)

    hist = historical_curve(list(seasons_hist), league.scoring)
    sim = simulated_curve(res)

    print(f"\nPositional finish curve -- simulated {season} vs actual {seasons_hist}")
    print(f"{'pos':<4}{'rank':>6}{'sim':>10}{'actual':>10}{'diff':>9}{'diff %':>9}")
    rows = []
    for pos in ("QB", "RB", "WR", "TE"):
        for r in RANKS:
            s = sim.get(pos, {}).get(r)
            h = hist.get(pos, {}).get(r)
            if s is None or h is None or not np.isfinite(h):
                continue
            print(f"{pos:<4}{r:>6}{s:>10.1f}{h:>10.1f}{s - h:>9.1f}{(s / h - 1) * 100:>8.1f}%")
            rows.append(s / h - 1)
    print(f"\nmean absolute deviation: {np.mean(np.abs(rows)) * 100:.1f}%")
    return res


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    main(n_sims=n)
