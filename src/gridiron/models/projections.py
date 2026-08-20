"""Assemble per-player projections for a season.

Pipeline
--------
1. Build the player pool from current rosters + depth charts.
2. Attach recency-weighted historical usage/efficiency profiles.
3. Fill missing roles from depth-chart priors (rookies, new signings).
4. Normalise usage *within each team* so shares are internally consistent.
5. Convert usage x efficiency x team context -> model points per game.
6. Blend with the market's implied value curve, weighted by role confidence,
   and fold the difference back into volume so the simulator stays coherent.

The output is a set of *distribution parameters*, not point estimates -- the
Monte Carlo engine consumes shares and rates, never a single projected number.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

import numpy as np
import polars as pl

from ..config import current_season
from ..data import nflverse as nv
from ..data.market import build_market_board, normalize_name
from ..features.players import POSITION_PRIORS, player_profiles
from ..features.team import TeamContext, implied_team_totals, team_context
from ..scoring import LeagueSettings

log = logging.getLogger(__name__)

#: Share of team targets by position and depth-chart rank, used when a player
#: has no usable history (rookies, position changes, new signings).
DEPTH_TARGET_PRIOR: dict[str, list[float]] = {
    "WR": [0.225, 0.170, 0.105, 0.055, 0.028, 0.014],
    "TE": [0.150, 0.052, 0.020, 0.010],
    "RB": [0.115, 0.060, 0.028, 0.012],
    "QB": [0.0],
    "FB": [0.020, 0.010],
}
DEPTH_CARRY_PRIOR: dict[str, list[float]] = {
    "RB": [0.520, 0.260, 0.115, 0.050, 0.020],
    "QB": [0.090, 0.040, 0.010],
    "WR": [0.018, 0.008, 0.004, 0.002],
    "TE": [0.004, 0.002],
    "FB": [0.030, 0.015],
}
DEPTH_DROPBACK_PRIOR: dict[str, list[float]] = {
    "QB": [0.930, 0.060, 0.008, 0.002],
    "RB": [0.001], "WR": [0.002], "TE": [0.0005], "FB": [0.0],
}

#: Rookies see the field less than their depth slot implies early on.
ROOKIE_USAGE_DISCOUNT = {"QB": 0.95, "RB": 0.90, "WR": 0.85, "TE": 0.70, "FB": 0.9}


def _prior_from_depth(table: dict[str, list[float]], pos: str, rank: float | None) -> float:
    arr = table.get(pos or "", [0.0])
    if rank is None or not np.isfinite(rank):
        idx = min(2, len(arr) - 1)
    else:
        idx = int(max(1, round(rank))) - 1
    idx = min(max(idx, 0), len(arr) - 1)
    base = arr[idx]
    # Beyond the listed depth, decay rather than clamp.
    if rank is not None and np.isfinite(rank) and int(rank) > len(arr):
        base *= 0.5 ** (int(rank) - len(arr))
    return float(base)


def isotonic_decreasing(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Pool-adjacent-violators fit of a non-increasing curve y(x).

    Used to turn "market rank -> projected points" into a smooth monotone
    mapping without importing scikit-learn.
    """
    order = np.argsort(x, kind="mergesort")
    yy = np.asarray(y, dtype=np.float64)[order].copy()
    w = np.ones_like(yy)
    # PAVA for a non-increasing sequence.
    level_y: list[float] = []
    level_w: list[float] = []
    for val, wt in zip(yy, w):
        level_y.append(val)
        level_w.append(wt)
        while len(level_y) > 1 and level_y[-2] < level_y[-1]:
            y2 = level_y.pop()
            w2 = level_w.pop()
            y1 = level_y.pop()
            w1 = level_w.pop()
            level_y.append((y1 * w1 + y2 * w2) / (w1 + w2))
            level_w.append(w1 + w2)
    fitted = np.repeat(level_y, [int(round(v)) for v in level_w])
    out = np.empty_like(yy)
    out[order] = fitted[: len(yy)]
    return out


@dataclass
class ProjectionSet:
    """Everything the simulator needs for one season."""

    players: pl.DataFrame
    team_weeks: pl.DataFrame
    season: int
    league: LeagueSettings
    context: TeamContext | None
    market_sources: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))

    def __len__(self) -> int:
        return self.players.height

    def summary(self) -> dict:
        out = {
            "season": self.season,
            "players": self.players.height,
            "team_weeks": self.team_weeks.height,
            "market_sources": self.market_sources,
            "generated_at": self.generated_at,
        }
        # A set hydrated from disk carries no team context.
        if self.context is not None:
            out["baselines"] = {k: round(v, 3) for k, v in self.context.baselines.items()}
        return out


def _age_years(birth_date: pl.Expr, season: int) -> pl.Expr:
    ref = dt.date(season, 9, 1)
    return (
        (pl.lit(ref) - birth_date.cast(pl.Date, strict=False)).dt.total_days() / 365.25
    ).alias("age")


def build_player_pool(season: int, lookback: int = 3) -> pl.DataFrame:
    """Roster + depth chart + history + market, one row per fantasy-relevant player."""
    roster = nv.current_rosters(season)
    if roster.is_empty():
        raise RuntimeError(f"no roster data available for {season}")
    roster = nv.team_abbr_fixes(roster, ["team"])
    roster = roster.filter(pl.col("player_id").is_not_null())
    if "status" in roster.columns:
        active = ["ACT", "RES", "DEV", "INA", "Active", "Reserve/Injured", "Injured Reserve"]
        roster = roster.filter(pl.col("status").is_null() | pl.col("status").is_in(active))
    roster = roster.with_columns(
        pl.col("position").replace({"FB": "RB"}).alias("position"),
        pl.col("player_name").map_elements(normalize_name, return_dtype=pl.Utf8).alias("merge_name"),
    )
    if "birth_date" in roster.columns:
        roster = roster.with_columns(_age_years(pl.col("birth_date"), season))
    else:
        roster = roster.with_columns(pl.lit(26.0).alias("age"))
    # Sort before dedup: keep="first" is only meaningful on a defined order, and
    # without one a player with two roster rows kept an arbitrary one, changing
    # which players survived between otherwise identical builds.
    roster = roster.sort(["player_id", "team", "position"]).unique(
        subset=["player_id"], keep="first", maintain_order=True)

    # Depth chart rank within position group.
    dc = nv.latest_depth_chart(season)
    if not dc.is_empty():
        dc = nv.team_abbr_fixes(dc, ["team"])
        dc = dc.filter(pl.col("position").is_in(["QB", "RB", "WR", "TE", "FB"]))
        dc = (dc.sort("depth_rank")
                .unique(subset=["player_id"], keep="first")
                .select(["player_id", "depth_rank"]))
        roster = roster.join(dc, on="player_id", how="left")
    else:
        roster = roster.with_columns(pl.lit(None, dtype=pl.Float64).alias("depth_rank"))

    profiles = player_profiles(season, lookback=lookback)
    if not profiles.is_empty():
        profiles = profiles.drop(["player_name", "merge_name", "position", "last_team"], strict=False)
        roster = roster.join(profiles, on="player_id", how="left")

    board = build_market_board()
    board_frame = board.frame
    if not board.frame.is_empty():
        cols = [c for c in ["merge_name", "adp", "adp_sd", "market_rank", "bye", "pos"]
                if c in board.frame.columns]
        mk = board.frame.select(cols).rename({"pos": "market_pos"})
        roster = roster.join(mk, on="merge_name", how="left")
    for col, dtype in (("adp", pl.Float64), ("adp_sd", pl.Float64), ("market_rank", pl.Float64),
                       ("bye", pl.Float64)):
        if col not in roster.columns:
            roster = roster.with_columns(pl.lit(None, dtype=dtype).alias(col))

    roster = roster.with_columns(
        pl.col("games").fill_null(0).alias("games"),
        pl.col("role_confidence").fill_null(0.0).alias("role_confidence"),
        (pl.col("seasons_played").fill_null(0) == 0).alias("is_rookie"),
    )
    return roster, board.sources, board_frame


def _fill_usage_priors(pool: pl.DataFrame) -> pl.DataFrame:
    """Blend observed usage shares with depth-chart priors by role confidence."""
    pos = pool["position"].to_list()
    rank = pool["depth_rank"].to_list()
    tgt_prior = np.array([_prior_from_depth(DEPTH_TARGET_PRIOR, p, r) for p, r in zip(pos, rank)])
    car_prior = np.array([_prior_from_depth(DEPTH_CARRY_PRIOR, p, r) for p, r in zip(pos, rank)])
    drop_prior = np.array([_prior_from_depth(DEPTH_DROPBACK_PRIOR, p, r) for p, r in zip(pos, rank)])
    rookie_disc = np.array([ROOKIE_USAGE_DISCOUNT.get(p, 0.85) for p in pos])
    is_rookie = pool["is_rookie"].to_numpy()
    tgt_prior = np.where(is_rookie, tgt_prior * rookie_disc, tgt_prior)
    car_prior = np.where(is_rookie, car_prior * rookie_disc, car_prior)

    conf = pool["role_confidence"].to_numpy()
    obs_t = np.nan_to_num(pool["target_share"].to_numpy(), nan=0.0)
    obs_c = np.nan_to_num(pool["carry_share"].to_numpy(), nan=0.0)
    obs_d = np.nan_to_num(pool["dropback_share"].to_numpy(), nan=0.0)
    obs_a = np.nan_to_num(pool["air_yards_share"].to_numpy(), nan=0.0)

    return pool.with_columns(
        pl.Series("raw_target_share", conf * obs_t + (1 - conf) * tgt_prior),
        pl.Series("raw_carry_share", conf * obs_c + (1 - conf) * car_prior),
        pl.Series("raw_dropback_share", conf * obs_d + (1 - conf) * drop_prior),
        pl.Series("raw_air_share", conf * obs_a + (1 - conf) * tgt_prior * 1.15),
    )


def _fill_efficiency(pool: pl.DataFrame) -> pl.DataFrame:
    """Any missing rate falls back to its positional prior."""
    exprs = []
    for rate in ("comp_pct", "yards_per_att", "pass_td_rate", "int_rate", "sack_rate",
                 "rush_ypc", "rush_td_rate", "catch_rate", "yards_per_target", "adot",
                 "rec_td_rate"):
        prior = pl.col("position").replace_strict(
            {p: v.get(rate, 0.0) for p, v in POSITION_PRIORS.items()},
            default=POSITION_PRIORS["WR"].get(rate, 0.0), return_dtype=pl.Float64)
        if rate in pool.columns:
            exprs.append(pl.col(rate).fill_null(prior).alias(rate))
        else:
            exprs.append(prior.alias(rate))
    if "fumble_rate" in pool.columns:
        exprs.append(pl.col("fumble_rate").fill_null(0.0055).alias("fumble_rate"))
    else:
        exprs.append(pl.lit(0.0055).alias("fumble_rate"))
    return pool.with_columns(exprs)


def _assign_qb_shares(pool: pl.DataFrame) -> pl.DataFrame:
    """Re-derive quarterback dropback shares from *who starts*, not history.

    A season-averaged dropback share is a trap at quarterback: it splits a
    team between its starter and whoever covered his injuries, and the sim then
    invents a timeshare that will not happen. Instead each team's quarterbacks
    are ranked by a starter score -- market rank first (the market prices
    starting jobs quickly), then depth-chart rank, then recent volume -- and the
    depth-based prior is applied to that ranking.
    """
    if pool.is_empty():
        return pool
    qb = pool.filter(pl.col("position") == "QB")
    if qb.is_empty():
        return pool

    big = 10_000.0
    scored = qb.with_columns(
        (
            pl.col("market_rank").fill_null(big)
            + pl.col("depth_rank").fill_null(9.0) * 50.0
            - pl.col("dropback_share").fill_null(0.0) * 200.0
            - pl.col("attempts_pg").fill_null(0.0) * 2.0
        ).alias("_starter_score")
    )
    scored = scored.with_columns(
        pl.col("_starter_score").rank("ordinal").over("team").alias("_qb_rank")
    )
    prior = DEPTH_DROPBACK_PRIOR["QB"]
    lookup = {i + 1: v for i, v in enumerate(prior)}
    scored = scored.with_columns(
        pl.col("_qb_rank").replace_strict(lookup, default=0.001, return_dtype=pl.Float64)
        .alias("_qb_share")
    )
    return pool.join(scored.select(["player_id", "_qb_share", "_qb_rank"]),
                     on="player_id", how="left").with_columns(
        pl.when(pl.col("position") == "QB")
        .then(pl.col("_qb_share"))
        .otherwise(pl.col("raw_dropback_share"))
        .alias("raw_dropback_share"),
        pl.col("_qb_rank").alias("qb_depth"),
    ).drop("_qb_share")


#: Mean share held by the *n*-th busiest player on a team, measured on
#: 2023-2025 season totals. Blending priors with observed per-game averages
#: produces a flatter curve than this, so shares are sharpened to match it.
ACTUAL_TARGET_CURVE = (0.237, 0.175, 0.133, 0.106, 0.086, 0.066, 0.053, 0.042)
ACTUAL_CARRY_CURVE = (0.490, 0.229, 0.114, 0.069, 0.037, 0.024, 0.015, 0.009)


def _sharpen(values: np.ndarray, teams: np.ndarray, target_curve, scale: float = 1.0,
             lo: float = 0.4, hi: float = 4.0, steps: int = 60) -> np.ndarray:
    """Raise shares to a power so the team-rank curve matches `target_curve`.

    Averaging a player's per-game share understates how concentrated a season
    actually is -- the team leader is the *max* of a set of noisy roles, not the
    max of their means. One exponent per category, chosen by least squares
    against the measured historical curve (not just its first point, which
    would overshoot the middle of the distribution), restores the right shape
    without hand-tuning any individual player.

    `scale` accounts for a slice of the pie held out of the fit: when
    quarterback carries are excluded, the remaining shares are compared against
    the historical curve divided by the share still available to them.
    """
    values = np.maximum(np.nan_to_num(values, nan=0.0), 0.0)
    uniq = np.unique(teams)
    target = np.asarray(target_curve, dtype=np.float64) / max(scale, 1e-6)
    k = len(target)

    def curve(gamma: float) -> np.ndarray:
        rows = []
        for t in uniq:
            v = values[teams == t] ** gamma
            total = v.sum()
            if total <= 0:
                continue
            top = np.sort(v)[::-1][:k] / total
            if len(top) < k:
                top = np.concatenate([top, np.zeros(k - len(top))])
            rows.append(top)
        return np.mean(rows, axis=0) if rows else np.zeros(k)

    best_gamma, best_err = 1.0, np.inf
    for gamma in np.linspace(lo, hi, steps):
        err = float(np.sum((curve(gamma) - target) ** 2))
        if err < best_err:
            best_gamma, best_err = float(gamma), err

    out = values ** best_gamma
    for t in uniq:
        sel = teams == t
        total = out[sel].sum()
        if total > 0:
            out[sel] = out[sel] / total
    return out


def _normalise_within_team(values: np.ndarray, teams: np.ndarray) -> np.ndarray:
    out = np.maximum(np.nan_to_num(values, nan=0.0), 0.0)
    for t in np.unique(teams):
        sel = teams == t
        total = out[sel].sum()
        if total > 0:
            out[sel] = out[sel] / total
    return out


def _concentrate_shares(pool: pl.DataFrame) -> pl.DataFrame:
    """Match the projected within-team share curves to the historical ones.

    Quarterback carries are held out of the rushing fit. Quarterbacks take 15.4%
    of team carries league-wide (sd 6.1%), and raising every share to a power
    greater than one to concentrate the *backfield* would quietly delete most of
    it -- which is exactly the production separating the elite fantasy
    quarterbacks from the rest.
    """
    teams = pool["team"].to_numpy()
    positions = pool["position"].to_numpy()

    carry = _normalise_within_team(pool["raw_carry_share"].to_numpy(), teams)
    is_qb = positions == "QB"
    qb_carry = np.where(is_qb, carry, 0.0)

    qb_total = np.zeros_like(carry)
    for t in np.unique(teams):
        sel = teams == t
        qb_total[sel] = float(np.clip(qb_carry[sel].sum(), 0.01, 0.32))

    nonqb = _normalise_within_team(np.where(is_qb, 0.0, carry), teams)
    mean_qb = float(np.mean([qb_total[teams == t][0] for t in np.unique(teams)]))
    nonqb = _sharpen(nonqb, teams, ACTUAL_CARRY_CURVE, scale=1.0 - mean_qb)
    out_carry = nonqb * (1.0 - qb_total) + qb_carry

    targets = _normalise_within_team(pool["raw_target_share"].to_numpy(), teams)
    air = _normalise_within_team(pool["raw_air_share"].to_numpy(), teams)
    return pool.with_columns(
        pl.Series("raw_target_share", _sharpen(targets, teams, ACTUAL_TARGET_CURVE)),
        pl.Series("raw_carry_share", out_carry),
        pl.Series("raw_air_share", _sharpen(air, teams, ACTUAL_TARGET_CURVE)),
    )


def _normalise_team_shares(pool: pl.DataFrame) -> pl.DataFrame:
    """Force each team's shares to sum to one across its own roster."""
    return pool.with_columns(
        (pl.col("raw_target_share") / pl.col("raw_target_share").sum().over("team").clip(1e-6))
        .alias("target_share_proj"),
        (pl.col("raw_carry_share") / pl.col("raw_carry_share").sum().over("team").clip(1e-6))
        .alias("carry_share_proj"),
        (pl.col("raw_dropback_share") / pl.col("raw_dropback_share").sum().over("team").clip(1e-6))
        .alias("dropback_share_proj"),
        (pl.col("raw_air_share") / pl.col("raw_air_share").sum().over("team").clip(1e-6))
        .alias("air_share_proj"),
    )


def _model_points_per_game(pool: pl.DataFrame, ctx: TeamContext, league: LeagueSettings,
                           team_week: pl.DataFrame) -> pl.DataFrame:
    """Deterministic expectation used only for market blending and display."""
    tw = team_week.group_by("team").agg(
        pl.col("exp_plays_game").mean().alias("t_plays"),
        pl.col("exp_pass_rate_neutral").mean().alias("t_pass_rate"),
        pl.col("implied_points").mean().alias("t_points"),
    )
    derived = ["t_plays", "t_pass_rate", "t_points", "team_attempts", "team_carries",
               "team_tds", "pass_att_pg", "carries_pg_proj", "targets_pg_proj",
               "pass_yds_pg", "pass_td_pg", "int_pg", "rush_yds_pg", "rush_td_pg",
               "rec_pg", "rec_yds_pg", "rec_td_pg", "model_ppg"]
    df = pool.drop([c for c in derived if c in pool.columns]).join(tw, on="team", how="left")
    b = ctx.baselines
    df = df.with_columns(
        pl.col("t_plays").fill_null(b["plays"]),
        pl.col("t_pass_rate").fill_null(b["pass_rate"]),
        pl.col("t_points").fill_null(b["points"]),
    )
    # Team opportunity pools per game.
    df = df.with_columns(
        (pl.col("t_plays") * pl.col("t_pass_rate") * (1 - pl.col("sack_rate").clip(0.0, 0.15)))
        .alias("team_attempts"),
        (pl.col("t_plays") * (1 - pl.col("t_pass_rate"))).alias("team_carries"),
    )
    # Team touchdowns implied by the scoring environment (~63% of points are TDs).
    df = df.with_columns(
        (pl.col("t_points") * 0.615 / 6.0).alias("team_tds"),
    )
    s = league.scoring
    df = df.with_columns(
        (pl.col("team_attempts") * pl.col("dropback_share_proj")).alias("pass_att_pg"),
        (pl.col("team_carries") * pl.col("carry_share_proj")).alias("carries_pg_proj"),
        (pl.col("team_attempts") * pl.col("target_share_proj")).alias("targets_pg_proj"),
    )
    pass_td_share = ctx.baselines.get("pass_td_rate", 0.62)
    df = df.with_columns(
        (pl.col("pass_att_pg") * pl.col("yards_per_att")).alias("pass_yds_pg"),
        (pl.col("team_tds") * pass_td_share * pl.col("dropback_share_proj")).alias("pass_td_pg"),
        (pl.col("pass_att_pg") * pl.col("int_rate")).alias("int_pg"),
        (pl.col("carries_pg_proj") * pl.col("rush_ypc")).alias("rush_yds_pg"),
        (pl.col("team_tds") * (1 - pass_td_share) * pl.col("carry_share_proj")).alias("rush_td_pg"),
        (pl.col("targets_pg_proj") * pl.col("catch_rate")).alias("rec_pg"),
        (pl.col("targets_pg_proj") * pl.col("yards_per_target")).alias("rec_yds_pg"),
        (pl.col("team_tds") * pass_td_share * pl.col("target_share_proj")).alias("rec_td_pg"),
    )
    ppg = (
        pl.col("pass_yds_pg") * s.passing_yards
        + pl.col("pass_td_pg") * s.passing_tds
        + pl.col("int_pg") * s.passing_interceptions
        + pl.col("rush_yds_pg") * s.rushing_yards
        + pl.col("rush_td_pg") * s.rushing_tds
        + pl.col("rec_pg") * s.receptions
        + pl.col("rec_yds_pg") * s.receiving_yards
        + pl.col("rec_td_pg") * s.receiving_tds
        + (pl.col("carries_pg_proj") + pl.col("rec_pg")) * pl.col("fumble_rate") * s.fumbles_lost
    )
    if s.te_premium:
        ppg = ppg + pl.col("rec_pg") * s.te_premium * (pl.col("position") == "TE").cast(pl.Float64)
    return df.with_columns(ppg.alias("model_ppg"))


def _market_blend(df: pl.DataFrame) -> pl.DataFrame:
    """Blend bottom-up points with the market's implied value curve.

    The curve is fit *within each position*: a monotone (isotonic) regression of
    model points-per-game against the player's rank among his own position in
    the market board. Fitting across positions would be wrong -- an overall
    board encodes positional scarcity (a QB going 107th is not a 107th-best
    scorer), so only the within-position ordering carries points information.

    Fitting on the pool itself keeps the blend self-calibrating in level: it
    imports the market's *ordering*, which knows about offseason role changes
    the box scores have not seen yet, without importing anyone's point totals.
    """
    mkt = df["market_rank"].to_numpy().astype(float)
    ppg = np.nan_to_num(df["model_ppg"].to_numpy(), nan=0.0)
    positions = df["position"].to_numpy()
    market_ppg = np.full(len(ppg), np.nan)

    for pos in np.unique(positions):
        sel = (positions == pos) & np.isfinite(mkt)
        if sel.sum() < 8:
            continue
        idx = np.flatnonzero(sel)
        # Rank within position (1 = best), derived from the overall board.
        # Stable sorts: every unranked player carries the same sentinel market
        # rank, so a quicksort permutes that whole tied block differently on
        # each run. The isotonic fit below is global, so that permutation moved
        # projections for most of the board between identical builds.
        pos_rank = np.argsort(np.argsort(mkt[idx], kind="stable"),
                              kind="stable").astype(float) + 1.0
        market_ppg[idx] = isotonic_decreasing(pos_rank, ppg[idx])

    conf = df["role_confidence"].to_numpy()
    # Weight on the bottom-up model: never fully trust either side. A player with
    # no usable history leans on the market; an entrenched starter leans on his
    # own tape.
    w_model = np.clip(0.35 + 0.5 * conf, 0.3, 0.85)
    blended = np.where(np.isfinite(market_ppg), w_model * ppg + (1 - w_model) * market_ppg, ppg)
    mult = np.where(ppg > 0.5, blended / np.maximum(ppg, 1e-6), 1.0)
    mult = np.clip(mult, 0.45, 2.2)
    df = df.drop([c for c in ("market_ppg", "volume_multiplier", "blended_ppg") if c in df.columns])
    return df.with_columns(
        pl.Series("market_ppg", market_ppg),
        pl.Series("volume_multiplier", mult),
        pl.Series("blended_ppg", blended),
    )


def build_projections(season: int | None = None, league: LeagueSettings | None = None,
                      lookback: int = 3, blend_iterations: int = 2) -> ProjectionSet:
    """Full projection build for `season`."""
    season = season or current_season()
    league = league or LeagueSettings()
    ctx = team_context(season, lookback=lookback)
    team_week = implied_team_totals(season, ctx)

    pool, sources, board_frame = build_player_pool(season, lookback=lookback)
    pool = _fill_usage_priors(pool)
    pool = _fill_efficiency(pool)
    pool = _assign_qb_shares(pool)
    pool = _concentrate_shares(pool)
    pool = _normalise_team_shares(pool)

    for _ in range(max(1, blend_iterations)):
        pool = _model_points_per_game(pool, ctx, league, team_week)
        pool = _market_blend(pool)
        mult = pool["volume_multiplier"].to_numpy()
        pool = pool.with_columns(
            pl.Series("raw_target_share", pool["raw_target_share"].to_numpy() * mult),
            pl.Series("raw_carry_share", pool["raw_carry_share"].to_numpy() * mult),
            pl.Series("raw_air_share", pool["raw_air_share"].to_numpy() * mult),
            # QB dropback share is a starter/backup decision, not a volume dial;
            # rescaling it would invent split-snap quarterbacks that do not exist.
        )
        pool = _normalise_team_shares(pool)
    pool = _model_points_per_game(pool, ctx, league, team_week)

    # Availability inputs.
    missed = 1.0 - np.nan_to_num(pool["games_prev_season"].to_numpy(), nan=np.nan) / 17.0
    pool = pool.with_columns(pl.Series("games_missed_rate", missed))

    keep = [c for c in [
        "player_id", "player_name", "merge_name", "position", "team", "age", "depth_rank",
        "is_rookie", "role_confidence", "games", "games_prev_season", "seasons_played",
        "adp", "adp_sd", "market_rank", "bye", "games_missed_rate",
        "target_share_proj", "carry_share_proj", "dropback_share_proj", "air_share_proj",
        "comp_pct", "yards_per_att", "pass_td_rate", "int_rate", "sack_rate",
        "rush_ypc", "rush_td_rate", "catch_rate", "yards_per_target", "adot", "rec_td_rate",
        "fumble_rate", "target_share_sd", "carry_share_sd",
        "pass_att_pg", "carries_pg_proj", "targets_pg_proj", "model_ppg", "market_ppg",
        "qb_depth",
        "blended_ppg", "volume_multiplier",
    ] if c in pool.columns]
    players = pool.select(keep)

    # Kickers and team defences come from their own (much simpler) models but
    # live in the same table so the draft tools see one unified pool.
    from .special import dst_projections, kicker_projections

    extras = []
    for builder in (kicker_projections, dst_projections):
        try:
            extra = builder(team_week, season, league)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s failed: %s", builder.__name__, exc)
            continue
        if extra.is_empty():
            continue
        extra = extra.with_columns(
            pl.col("player_name").map_elements(normalize_name, return_dtype=pl.Utf8).alias("merge_name"),
            pl.col("model_ppg").alias("blended_ppg"),
        )
        if not board_frame.is_empty():
            mk = board_frame.select([c for c in ("merge_name", "adp", "adp_sd", "market_rank")
                                     if c in board_frame.columns])
            extra = extra.join(mk, on="merge_name", how="left")
        extras.append(extra)

    if extras:
        # Kickers and defences are modelled in special.py, but the roster pool
        # also contains kickers — concatenating both put 29 players on the board
        # twice, where they double-counted in every team-level aggregation.
        # The specialised model is the intended source, so the pool's copies go.
        specialised = {"K", "DST"}
        players = players.filter(~pl.col("position").is_in(list(specialised)))
        players = pl.concat([players] + extras, how="diagonal_relaxed")
        players = players.unique(subset=["player_id"], keep="first", maintain_order=True)
    # player_id breaks ties so the ordering is total. Without it, players on
    # equal projected points order arbitrarily, which shuffles their index in
    # the simulation arrays and therefore which random draws they receive —
    # enough to move a backtest metric in the third decimal between runs.
    players = players.sort(["blended_ppg", "player_id"],
                           descending=[True, False], nulls_last=True)
    return ProjectionSet(players=players, team_weeks=team_week, season=season,
                         league=league, context=ctx, market_sources=sources)
