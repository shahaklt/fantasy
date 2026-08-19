"""Value-based drafting: replacement levels, VORP/VOLS/VONA, tiers, auction $.

The projection tells you how many points a player scores. Value-based drafting
tells you how many points he *wins you*, which is the only quantity a draft pick
should be judged on: points above the player you could have had instead.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from ..scoring import LeagueSettings

#: Positions that are effectively free on waivers all year.
STREAMABLE = ("K", "DST")


@dataclass
class ReplacementLevels:
    """Points-per-season of the replacement player at each position."""

    vorp: dict[str, float]     # last startable player league-wide
    vols: dict[str, float]     # worst *starter* on any roster
    counts: dict[str, float]   # how many at that position get drafted as starters

    def as_dict(self) -> dict:
        return {"vorp": self.vorp, "vols": self.vols, "counts": self.counts}


def replacement_levels(board: pl.DataFrame, league: LeagueSettings,
                       points_col: str = "proj_points") -> ReplacementLevels:
    """Compute VORP and VOLS baselines from league size and roster slots.

    * **VOLS** ("value over last starter") uses exactly the number of starters
      the league demands -- the strict definition of scarcity at draft time.
    * **VORP** adds the bench depth that realistically gets started at that
      position over a season, which is the more useful baseline in practice
      because injuries and byes force everyone deeper down their own bench.
    """
    demand = league.positional_demand()
    vorp, vols, counts = {}, {}, {}
    # How far past the last required starter people realistically end up
    # starting at each position over a season of byes and injuries.
    bench_factor = {"QB": 0.25, "RB": 0.50, "WR": 0.40, "TE": 0.25, "K": 0.0, "DST": 0.0}

    for pos, starters in demand.items():
        sub = board.filter(pl.col("position") == pos).sort(points_col, descending=True)
        pts = sub[points_col].to_numpy() if sub.height else np.array([0.0])
        n_start = max(int(round(starters)), 1)
        n_repl = max(int(round(starters * (1.0 + bench_factor.get(pos, 0.5)))), n_start)

        def at(idx: int) -> float:
            if len(pts) == 0:
                return 0.0
            return float(pts[min(max(idx - 1, 0), len(pts) - 1)])

        vols[pos] = at(n_start)
        vorp[pos] = at(n_repl)
        counts[pos] = float(n_start)
    return ReplacementLevels(vorp=vorp, vols=vols, counts=counts)


def add_value_columns(board: pl.DataFrame, league: LeagueSettings,
                      points_col: str = "proj_points") -> pl.DataFrame:
    """Attach VORP / VOLS and an auction dollar value to a projection board."""
    levels = replacement_levels(board, league, points_col)
    vorp_map = pl.col("position").replace_strict(levels.vorp, default=0.0, return_dtype=pl.Float64)
    vols_map = pl.col("position").replace_strict(levels.vols, default=0.0, return_dtype=pl.Float64)

    out = board.with_columns(
        (pl.col(points_col) - vorp_map).alias("vorp"),
        (pl.col(points_col) - vols_map).alias("vols"),
    )
    return _auction_values(out, league)


def _auction_values(board: pl.DataFrame, league: LeagueSettings,
                    value_col: str = "vorp") -> pl.DataFrame:
    """Convert VORP into auction dollars.

    Total money in the room is ``teams x budget``. Every drafted player costs at
    least $1, so only the surplus above ``roster_size x $1`` is distributed in
    proportion to positive value over replacement.
    """
    n_drafted = league.total_picks
    total_budget = league.teams * league.auction_budget
    surplus = max(total_budget - n_drafted, 1)

    ranked = board.sort(value_col, descending=True, nulls_last=True)
    val = np.nan_to_num(ranked[value_col].to_numpy(), nan=0.0)
    drafted = np.zeros(len(val), dtype=bool)
    drafted[: min(n_drafted, len(val))] = True
    pos_val = np.where(drafted & (val > 0), val, 0.0)
    total_val = pos_val.sum()
    dollars = np.where(drafted, 1.0 + (pos_val / total_val) * surplus if total_val > 0 else 1.0, 0.0)
    return ranked.with_columns(pl.Series("auction_value", np.round(dollars, 1)))


def assign_tiers(board: pl.DataFrame, value_col: str = "vorp",
                 max_tiers: int = 12, min_gap_sd: float = 0.55) -> pl.DataFrame:
    """Group each position into tiers by finding real gaps in value.

    A tier break is declared where the drop to the next player is large relative
    to the typical drop at that position -- which is what "tiers" means in
    practice: a cliff you should not let yourself fall off.
    """
    frames = []
    for (pos,), sub in board.group_by(["position"], maintain_order=True):
        sub = sub.sort(value_col, descending=True, nulls_last=True)
        vals = np.nan_to_num(sub[value_col].to_numpy(), nan=0.0)
        if len(vals) < 2:
            frames.append(sub.with_columns(pl.lit(1).alias("tier")))
            continue
        gaps = -np.diff(vals)
        gaps = np.maximum(gaps, 0.0)
        threshold = gaps.mean() + min_gap_sd * gaps.std()
        tier = np.ones(len(vals), dtype=np.int32)
        current = 1
        for i, g in enumerate(gaps):
            if g > threshold and current < max_tiers:
                current += 1
            tier[i + 1] = current
        frames.append(sub.with_columns(pl.Series("tier", tier)))
    return pl.concat(frames, how="diagonal_relaxed").sort(value_col, descending=True, nulls_last=True)


def positional_scarcity(board: pl.DataFrame, league: LeagueSettings,
                        drafted_ids: set[str] | None = None,
                        points_col: str = "proj_points") -> pl.DataFrame:
    """How fast value is falling off at each position right now.

    ``drop_to_next_tier`` is the VONA idea generalised: the points you lose by
    waiting for your next pick at this position rather than taking the best one
    now.
    """
    drafted_ids = drafted_ids or set()
    avail = board.filter(~pl.col("player_id").is_in(list(drafted_ids)))
    rows = []
    for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
        sub = avail.filter(pl.col("position") == pos).sort(points_col, descending=True)
        if sub.is_empty():
            continue
        pts = sub[points_col].to_numpy()
        best = float(pts[0])
        rows.append({
            "position": pos,
            "available": int(sub.height),
            "best_available": best,
            "drop_next": best - float(pts[1]) if len(pts) > 1 else 0.0,
            "drop_5": best - float(pts[min(5, len(pts) - 1)]),
            "drop_12": best - float(pts[min(12, len(pts) - 1)]),
            "top_tier_left": int((sub["tier"] == sub["tier"][0]).sum()) if "tier" in sub.columns else 0,
        })
    return pl.DataFrame(rows)
