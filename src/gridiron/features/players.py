"""Player usage + efficiency profiles.

The projection philosophy is *volume first*: opportunities (dropbacks, carries,
targets) are far more stable year over year than the efficiency applied to them,
so we estimate share-of-team-opportunity with heavy shrinkage and then apply
per-opportunity rates that are shrunk even harder toward positional means.
"""
from __future__ import annotations

import numpy as np
import polars as pl

from ..config import MIN_TRAIN_SEASON, current_season
from ..data import nflverse as nv
from ..data.market import normalize_name
from .team import SEASON_DECAY, _reg_season

#: Positional priors for per-opportunity efficiency; used as the shrinkage target.
POSITION_PRIORS: dict[str, dict[str, float]] = {
    "QB": {"comp_pct": 0.650, "yards_per_att": 7.05, "pass_td_rate": 0.045, "int_rate": 0.024,
           "sack_rate": 0.068, "rush_ypc": 4.30, "rush_td_rate": 0.035, "yards_per_target": 0.0,
           "catch_rate": 0.0, "adot": 0.0, "rec_td_rate": 0.0},
    "RB": {"rush_ypc": 4.30, "rush_td_rate": 0.030, "catch_rate": 0.760, "yards_per_target": 6.10,
           "adot": 0.6, "rec_td_rate": 0.017, "comp_pct": 0.0, "yards_per_att": 0.0,
           "pass_td_rate": 0.0, "int_rate": 0.0, "sack_rate": 0.0},
    "WR": {"rush_ypc": 6.50, "rush_td_rate": 0.060, "catch_rate": 0.625, "yards_per_target": 8.30,
           "adot": 11.0, "rec_td_rate": 0.058, "comp_pct": 0.0, "yards_per_att": 0.0,
           "pass_td_rate": 0.0, "int_rate": 0.0, "sack_rate": 0.0},
    "TE": {"rush_ypc": 3.50, "rush_td_rate": 0.030, "catch_rate": 0.685, "yards_per_target": 7.30,
           "adot": 8.2, "rec_td_rate": 0.060, "comp_pct": 0.0, "yards_per_att": 0.0,
           "pass_td_rate": 0.0, "int_rate": 0.0, "sack_rate": 0.0},
}
POSITION_PRIORS["FB"] = POSITION_PRIORS["RB"]

#: Prior sample size (in opportunities) for each efficiency rate.
EFFICIENCY_PRIOR_N: dict[str, float] = {
    "comp_pct": 220.0, "yards_per_att": 260.0, "pass_td_rate": 420.0, "int_rate": 450.0,
    "sack_rate": 300.0, "rush_ypc": 130.0, "rush_td_rate": 180.0, "catch_rate": 55.0,
    "yards_per_target": 70.0, "adot": 45.0, "rec_td_rate": 130.0,
}

#: Prior games for usage shares (how many league-average games we pretend to have seen).
USAGE_PRIOR_GAMES = 4.0


def _f(col: str) -> pl.Expr:
    return pl.col(col).cast(pl.Float64).fill_null(0.0)


def player_week_features(seasons: list[int] | None = None) -> pl.DataFrame:
    """One row per player-game with usage shares and raw opportunity counts."""
    seasons = seasons or list(range(MIN_TRAIN_SEASON, current_season() + 1))
    ps = _reg_season(nv.load_player_stats(seasons))
    if ps.is_empty():
        return ps

    keep = ["player_id", "player_display_name", "player_name", "position", "season", "week",
            "team", "opponent_team", "game_id", "completions", "attempts", "passing_yards",
            "passing_tds", "passing_interceptions", "sacks_suffered", "passing_air_yards",
            "carries", "rushing_yards", "rushing_tds", "receptions", "targets",
            "receiving_yards", "receiving_tds", "receiving_air_yards",
            "receiving_yards_after_catch", "target_share", "air_yards_share", "wopr",
            "rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost"]
    keep = [c for c in keep if c in ps.columns]
    ps = ps.select(keep)
    if "player_display_name" in ps.columns:
        ps = ps.drop("player_name", strict=False).rename({"player_display_name": "player_name"})

    num_cols = [c for c in ps.columns if c not in
                ("player_id", "player_name", "player_display_name", "position", "team",
                 "opponent_team", "game_id")]
    ps = ps.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in num_cols])
    ps = ps.filter(pl.col("position").is_in(["QB", "RB", "WR", "TE", "FB"]))

    # Team totals for the same game, from the player rows themselves (guarantees
    # shares sum to 1 even when the team stats feed lags).
    team_tot = ps.group_by(["season", "week", "team"]).agg(
        _f("attempts").sum().alias("team_attempts"),
        _f("carries").sum().alias("team_carries"),
        _f("targets").sum().alias("team_targets"),
        _f("receiving_air_yards").sum().alias("team_air_yards"),
        _f("passing_tds").sum().alias("team_pass_tds"),
        _f("rushing_tds").sum().alias("team_rush_tds"),
        _f("passing_yards").sum().alias("team_pass_yards"),
        _f("rushing_yards").sum().alias("team_rush_yards"),
        _f("sacks_suffered").sum().alias("team_sacks"),
    )
    df = ps.join(team_tot, on=["season", "week", "team"], how="left")

    df = df.with_columns(
        (_f("targets") / pl.col("team_targets").clip(1.0)).alias("target_share_calc"),
        (_f("carries") / pl.col("team_carries").clip(1.0)).alias("carry_share"),
        (_f("attempts") / pl.col("team_attempts").clip(1.0)).alias("dropback_share"),
        (_f("receiving_air_yards") / pl.col("team_air_yards").clip(1.0)).alias("air_yards_share_calc"),
        (_f("receiving_tds") / pl.col("team_pass_tds").clip(1.0)).alias("rec_td_share"),
        (_f("rushing_tds") / pl.col("team_rush_tds").clip(1.0)).alias("rush_td_share"),
    )

    # Snap share (PFR) when available -- the cleanest signal of role.
    try:
        snaps = _reg_season(nv.load_snap_counts(seasons))
        if not snaps.is_empty() and "offense_pct" in snaps.columns:
            snaps = snaps.select(
                pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64),
                pl.col("team"), pl.col("player").alias("_snap_name"),
                pl.col("offense_pct").cast(pl.Float64).alias("snap_pct"),
                pl.col("offense_snaps").cast(pl.Float64).alias("offense_snaps"),
            ).with_columns(
                pl.col("_snap_name").map_elements(normalize_name, return_dtype=pl.Utf8).alias("merge_name")
            )
            snaps = nv.team_abbr_fixes(snaps, ["team"])
            df = df.with_columns(
                pl.col("player_name").map_elements(normalize_name, return_dtype=pl.Utf8).alias("merge_name"),
                pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64),
            )
            df = nv.team_abbr_fixes(df, ["team", "opponent_team"])
            df = df.join(snaps.select(["season", "week", "team", "merge_name", "snap_pct", "offense_snaps"]),
                         on=["season", "week", "team", "merge_name"], how="left")
            # nflverse reports offense_pct as a fraction in recent seasons, % in old ones.
            df = df.with_columns(
                pl.when(pl.col("snap_pct") > 1.5).then(pl.col("snap_pct") / 100.0)
                .otherwise(pl.col("snap_pct")).alias("snap_pct")
            )
    except Exception:  # noqa: BLE001 - snaps are a nice-to-have
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("snap_pct"))

    if "merge_name" not in df.columns:
        df = df.with_columns(
            pl.col("player_name").map_elements(normalize_name, return_dtype=pl.Utf8).alias("merge_name")
        )
    if "snap_pct" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("snap_pct"))
    return df


def _weighted(df: pl.DataFrame, value: str, weight: str, alias: str) -> pl.Expr:
    return ((pl.col(value) * pl.col(weight)).sum() / pl.col(weight).sum().clip(1e-9)).alias(alias)


def player_profiles(target_season: int | None = None, lookback: int = 3,
                    min_games: int = 1) -> pl.DataFrame:
    """Recency-weighted, shrunk per-player usage and efficiency profile.

    Returns one row per player with:
      * ``games``, ``weighted_games`` -- sample size
      * usage shares (``target_share``, ``carry_share``, ``dropback_share``, ``snap_pct``)
      * efficiency rates (``yards_per_target``, ``rush_ypc``, ``catch_rate``, ...)
      * ``role_confidence`` in [0,1] -- how much the history should be trusted
    """
    target_season = target_season or current_season()
    seasons = [s for s in range(target_season - lookback, target_season + 1) if s >= MIN_TRAIN_SEASON]
    pw = player_week_features(seasons)
    if pw.is_empty():
        return pl.DataFrame()

    age = np.maximum(target_season - pw["season"].to_numpy().astype(float), 0.0)
    pw = pw.with_columns(pl.Series("w", np.power(SEASON_DECAY, age)))

    # Only count games where the player was actually active on offence.
    pw = pw.filter(
        (_f("attempts") + _f("carries") + _f("targets") > 0)
        | (pl.col("snap_pct").fill_null(0.0) > 0.05)
    )

    agg = pw.group_by("player_id").agg(
        pl.col("player_name").last().alias("player_name"),
        pl.col("merge_name").last().alias("merge_name"),
        pl.col("position").last().alias("position"),
        pl.col("team").last().alias("last_team"),
        pl.col("season").max().alias("last_season"),
        pl.len().alias("games"),
        pl.col("w").sum().alias("weighted_games"),
        pl.col("week").filter(pl.col("season") == target_season - 1).len().alias("games_prev_season"),
        pl.col("season").n_unique().alias("seasons_played"),
        # --- usage (weighted per-game means) ---
        _weighted(pw, "target_share_calc", "w", "target_share"),
        _weighted(pw, "air_yards_share_calc", "w", "air_yards_share"),
        _weighted(pw, "carry_share", "w", "carry_share"),
        _weighted(pw, "dropback_share", "w", "dropback_share"),
        _weighted(pw, "rec_td_share", "w", "rec_td_share"),
        _weighted(pw, "rush_td_share", "w", "rush_td_share"),
        ((pl.col("snap_pct").fill_null(0.0) * pl.col("w")).sum()
         / pl.col("w").sum().clip(1e-9)).alias("snap_pct"),
        _weighted(pw, "targets", "w", "targets_pg"),
        _weighted(pw, "carries", "w", "carries_pg"),
        _weighted(pw, "attempts", "w", "attempts_pg"),
        # --- efficiency numerators / denominators (weighted totals) ---
        (_f("completions") * pl.col("w")).sum().alias("_comp"),
        (_f("attempts") * pl.col("w")).sum().alias("_att"),
        (_f("passing_yards") * pl.col("w")).sum().alias("_pass_yds"),
        (_f("passing_tds") * pl.col("w")).sum().alias("_pass_td"),
        (_f("passing_interceptions") * pl.col("w")).sum().alias("_int"),
        (_f("sacks_suffered") * pl.col("w")).sum().alias("_sacks"),
        (_f("carries") * pl.col("w")).sum().alias("_car"),
        (_f("rushing_yards") * pl.col("w")).sum().alias("_rush_yds"),
        (_f("rushing_tds") * pl.col("w")).sum().alias("_rush_td"),
        (_f("targets") * pl.col("w")).sum().alias("_tgt"),
        (_f("receptions") * pl.col("w")).sum().alias("_rec"),
        (_f("receiving_yards") * pl.col("w")).sum().alias("_rec_yds"),
        (_f("receiving_tds") * pl.col("w")).sum().alias("_rec_td"),
        (_f("receiving_air_yards") * pl.col("w")).sum().alias("_air"),
        ((_f("rushing_fumbles_lost") + _f("receiving_fumbles_lost") + _f("sack_fumbles_lost"))
         * pl.col("w")).sum().alias("_fum"),
        # --- volatility of usage, used later to widen the simulated distribution ---
        pl.col("target_share_calc").std().fill_null(0.0).alias("target_share_sd"),
        pl.col("carry_share").std().fill_null(0.0).alias("carry_share_sd"),
    ).filter(pl.col("games") >= min_games)

    agg = _shrink_efficiency(agg)

    # Role confidence: recent, high-volume, stable usage -> trust the history.
    agg = agg.with_columns(
        (
            (pl.col("weighted_games") / (pl.col("weighted_games") + 6.0))
            * pl.when(pl.col("last_season") >= target_season - 1).then(1.0).otherwise(0.45)
        ).clip(0.0, 1.0).alias("role_confidence")
    )
    drop = [c for c in agg.columns if c.startswith("_")]
    return agg.drop(drop).sort("player_name")


def _shrink_efficiency(agg: pl.DataFrame) -> pl.DataFrame:
    """Empirical-Bayes shrink each rate toward its positional prior."""
    prior_cols = {}
    for rate in EFFICIENCY_PRIOR_N:
        prior_cols[rate] = pl.col("position").replace_strict(
            {pos: vals.get(rate, 0.0) for pos, vals in POSITION_PRIORS.items()},
            default=POSITION_PRIORS["WR"].get(rate, 0.0),
            return_dtype=pl.Float64,
        )

    def eb(num: pl.Expr, den: pl.Expr, rate: str) -> pl.Expr:
        n0 = EFFICIENCY_PRIOR_N[rate]
        prior = prior_cols[rate]
        return ((num + prior * n0) / (den + n0)).alias(rate)

    dropbacks = pl.col("_att") + pl.col("_sacks")
    return agg.with_columns(
        eb(pl.col("_comp"), pl.col("_att"), "comp_pct"),
        eb(pl.col("_pass_yds"), pl.col("_att"), "yards_per_att"),
        eb(pl.col("_pass_td"), pl.col("_att"), "pass_td_rate"),
        eb(pl.col("_int"), pl.col("_att"), "int_rate"),
        eb(pl.col("_sacks"), dropbacks, "sack_rate"),
        eb(pl.col("_rush_yds"), pl.col("_car"), "rush_ypc"),
        eb(pl.col("_rush_td"), pl.col("_car"), "rush_td_rate"),
        eb(pl.col("_rec"), pl.col("_tgt"), "catch_rate"),
        eb(pl.col("_rec_yds"), pl.col("_tgt"), "yards_per_target"),
        eb(pl.col("_air"), pl.col("_tgt"), "adot"),
        eb(pl.col("_rec_td"), pl.col("_tgt"), "rec_td_rate"),
        ((pl.col("_fum") + 0.6) / (pl.col("_car") + pl.col("_rec") + dropbacks + 220.0)).alias("fumble_rate"),
    )
