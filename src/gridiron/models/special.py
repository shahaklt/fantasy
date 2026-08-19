"""Kicker and team-defence projections.

Both are driven by the same Vegas-implied scoring environment the skill-position
model uses, which keeps them correlated with the rest of the slate: a shootout
lifts the kickers and hurts both defences, exactly as it does in reality.

Constants below were fit on 2021-2025 nflverse data:
  * FG attempts per game  = 1.632 + 0.0150 * team points
  * PAT attempts per game = -0.635 + 0.1281 * team points
  * FG distance mix       = 50.4% under 40, 27.9% 40-49, 21.7% 50+
  * make rates            = .965 / .817 / .708
  * defence per game      = 2.39 sacks, 0.76 INT, 0.50 fumble recoveries,
                            0.066 defensive TDs, 0.024 safeties
"""
from __future__ import annotations

import numpy as np
import polars as pl

from ..data import nflverse as nv
from ..scoring import LeagueSettings

FG_ATT_INTERCEPT, FG_ATT_SLOPE = 1.632, 0.0150
PAT_ATT_INTERCEPT, PAT_ATT_SLOPE = -0.635, 0.1281
FG_DISTANCE_MIX = {"0_39": 0.504, "40_49": 0.279, "50_plus": 0.217}
FG_MAKE_RATE = {"0_39": 0.965, "40_49": 0.817, "50_plus": 0.708}

DST_BASE = {"sacks": 2.393, "interceptions": 0.756, "fumble_recoveries": 0.505,
            "tds": 0.0662, "safeties": 0.0239}


def kicker_projections(team_weeks: pl.DataFrame, season: int,
                       league: LeagueSettings) -> pl.DataFrame:
    """One row per kicker with per-game FG/PAT expectations."""
    roster = nv.load_rosters([season])
    name_col = "full_name" if "full_name" in roster.columns else "player_name"
    ks = roster.filter(pl.col("position") == "K").select(
        pl.col("gsis_id").alias("player_id"),
        pl.col(name_col).alias("player_name"),
        pl.col("team"),
    )
    ks = nv.team_abbr_fixes(ks, ["team"]).unique(subset=["player_id"])
    if ks.is_empty():
        return pl.DataFrame()
    # One kicker per team: the depth chart rarely lists a second.
    ks = ks.unique(subset=["team"], keep="first")

    tw = team_weeks.group_by("team").agg(pl.col("implied_points").mean().alias("t_points"))
    df = ks.join(tw, on="team", how="left").with_columns(
        pl.col("t_points").fill_null(22.5)
    )
    df = df.with_columns(
        (FG_ATT_INTERCEPT + FG_ATT_SLOPE * pl.col("t_points")).clip(0.8, 3.5).alias("fg_att_pg"),
        (PAT_ATT_INTERCEPT + PAT_ATT_SLOPE * pl.col("t_points")).clip(0.3, 5.0).alias("pat_att_pg"),
    )
    s = league.scoring
    exp_fg_points = (
        FG_DISTANCE_MIX["0_39"] * FG_MAKE_RATE["0_39"] * s.fg_0_39
        + FG_DISTANCE_MIX["40_49"] * FG_MAKE_RATE["40_49"] * s.fg_40_49
        + FG_DISTANCE_MIX["50_plus"] * FG_MAKE_RATE["50_plus"] * s.fg_50_plus
        + (1 - sum(FG_DISTANCE_MIX[k] * FG_MAKE_RATE[k] for k in FG_MAKE_RATE)) * s.fg_miss
    )
    return df.with_columns(
        pl.lit("K").alias("position"),
        (pl.col("fg_att_pg") * exp_fg_points + pl.col("pat_att_pg") * 0.96 * s.pat_made)
        .alias("model_ppg"),
    )


def dst_projections(team_weeks: pl.DataFrame, season: int,
                    league: LeagueSettings) -> pl.DataFrame:
    """One row per team defence, scaled by the points it is expected to allow."""
    opp = team_weeks.select(["team", "opponent", "implied_points"]).rename(
        {"team": "_off", "opponent": "team", "implied_points": "points_allowed"}
    )
    df = opp.group_by("team").agg(pl.col("points_allowed").mean().alias("points_allowed"))
    if df.is_empty():
        return pl.DataFrame()

    league_pa = float(df["points_allowed"].mean())
    # A good defence gets more of everything; scale the base rates by how far
    # below league-average scoring it holds opponents.
    df = df.with_columns(
        ((league_pa / pl.col("points_allowed").clip(6.0, 40.0)) ** 0.6).alias("quality")
    )
    s = league.scoring
    pa = pl.col("points_allowed")
    # Expected points-allowed bucket score, integrating a normal around the mean.
    pa_score = (
        pl.when(pa < 14).then(s.dst_points_allowed_7_13)
        .when(pa < 21).then(s.dst_points_allowed_14_20)
        .when(pa < 28).then(s.dst_points_allowed_21_27)
        .when(pa < 35).then(s.dst_points_allowed_28_34)
        .otherwise(s.dst_points_allowed_35_plus)
    )
    df = df.with_columns(
        (DST_BASE["sacks"] * pl.col("quality")).alias("sacks_pg"),
        (DST_BASE["interceptions"] * pl.col("quality")).alias("int_pg"),
        (DST_BASE["fumble_recoveries"] * pl.col("quality")).alias("fum_pg"),
        (DST_BASE["tds"] * pl.col("quality")).alias("def_td_pg"),
        pl.lit(DST_BASE["safeties"]).alias("safety_pg"),
        pa_score.alias("pa_score"),
    )
    return df.with_columns(
        pl.col("team").alias("player_id"),
        (pl.col("team") + pl.lit(" D/ST")).alias("player_name"),
        pl.lit("DST").alias("position"),
        (
            pl.col("sacks_pg") * s.dst_sack
            + pl.col("int_pg") * s.dst_interception
            + pl.col("fum_pg") * s.dst_fumble_recovery
            + pl.col("def_td_pg") * s.dst_td
            + pl.col("safety_pg") * s.dst_safety
            + pl.col("pa_score")
        ).alias("model_ppg"),
    )
