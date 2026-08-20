"""Team-level context: pace, pass rate, scoring environment, defensive strength.

Everything here is built as *shrunk* estimates: a team's own recent history is
blended toward the league mean with a weight set by sample size, which is what
keeps week-1 projections from chasing noise.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from ..config import current_season
from ..data import nflverse as nv

# Prior strength (in team-games) used when shrinking a team's rate toward league mean.
PACE_PRIOR_GAMES = 6.0
PASS_RATE_PRIOR_GAMES = 8.0
DEF_PRIOR_GAMES = 10.0
#: Exponential recency decay applied per season of age.
SEASON_DECAY = 0.55


def _reg_season(df: pl.DataFrame) -> pl.DataFrame:
    if "season_type" in df.columns:
        return df.filter(pl.col("season_type") == "REG")
    if "game_type" in df.columns:
        return df.filter(pl.col("game_type") == "REG")
    return df


def team_game_features(seasons: list[int]) -> pl.DataFrame:
    """One row per team-game: offensive volume, pass rate, points for/against."""
    ts = _reg_season(nv.load_team_stats(seasons))
    sched = _reg_season(nv.load_schedules(seasons))

    cols = ["season", "week", "team", "opponent_team", "attempts", "carries",
            "sacks_suffered", "passing_yards", "rushing_yards", "targets",
            "receptions", "passing_tds", "rushing_tds", "receiving_tds",
            "passing_air_yards", "passing_interceptions"]
    cols = [c for c in cols if c in ts.columns]
    ts = ts.select(cols).with_columns(
        [pl.col(c).cast(pl.Float64).fill_null(0.0) for c in cols
         if c not in ("season", "week", "team", "opponent_team")]
    )
    sacks = pl.col("sacks_suffered") if "sacks_suffered" in ts.columns else pl.lit(0.0)
    ts = ts.with_columns(
        (pl.col("attempts") + sacks).alias("dropbacks"),
        (pl.col("attempts") + sacks + pl.col("carries")).alias("plays"),
    ).with_columns(
        (pl.col("dropbacks") / pl.col("plays").clip(1.0)).alias("pass_rate"),
    )

    # Points scored / allowed, joined from the schedule (long form).
    home = sched.select(
        pl.col("season"), pl.col("week"), pl.col("home_team").alias("team"),
        pl.col("home_score").cast(pl.Float64).alias("points_for"),
        pl.col("away_score").cast(pl.Float64).alias("points_against"),
        pl.col("spread_line").cast(pl.Float64).alias("team_spread"),
        pl.col("total_line").cast(pl.Float64).alias("total_line"),
        pl.lit(1).alias("is_home"),
    )
    away = sched.select(
        pl.col("season"), pl.col("week"), pl.col("away_team").alias("team"),
        pl.col("away_score").cast(pl.Float64).alias("points_for"),
        pl.col("home_score").cast(pl.Float64).alias("points_against"),
        (-pl.col("spread_line").cast(pl.Float64)).alias("team_spread"),
        pl.col("total_line").cast(pl.Float64).alias("total_line"),
        pl.lit(0).alias("is_home"),
    )
    games = pl.concat([home, away])
    out = ts.join(games, on=["season", "week", "team"], how="left")
    return out


def league_baselines(seasons: list[int]) -> dict[str, float]:
    """League-average offensive environment over `seasons` (recency weighted)."""
    tg = team_game_features(seasons)
    if tg.is_empty():
        return {"plays": 63.0, "pass_rate": 0.575, "points": 22.5, "attempts": 33.5,
                "carries": 26.5, "pass_td_rate": 0.62, "targets": 33.5}
    w = _season_weights(tg["season"].to_numpy(), max(seasons))
    def wm(col: str) -> float:
        v = tg[col].to_numpy().astype(float)
        m = np.isfinite(v)
        return float(np.average(v[m], weights=w[m])) if m.any() else float("nan")

    pass_td = wm("passing_tds")
    rush_td = wm("rushing_tds")
    return {
        "plays": wm("plays"),
        "pass_rate": wm("pass_rate"),
        "points": wm("points_for"),
        "attempts": wm("attempts"),
        "carries": wm("carries"),
        "targets": wm("targets"),
        "receptions": wm("receptions"),
        "passing_yards": wm("passing_yards"),
        "rushing_yards": wm("rushing_yards"),
        "pass_td_rate": float(pass_td / max(pass_td + rush_td, 1e-6)),
        "tds_per_game": float(pass_td + rush_td),
    }


def _season_weights(seasons: np.ndarray, target_season: int) -> np.ndarray:
    """Exponential recency weights: last season counts ~2x the one before."""
    age = np.maximum(target_season - seasons.astype(float), 0.0)
    return np.power(SEASON_DECAY, age)


@dataclass
class TeamContext:
    """Projected offensive environment for every team in `season`."""

    frame: pl.DataFrame  # team, exp_plays, pass_rate, def_pass_rate_allowed, ...
    baselines: dict[str, float]
    season: int

    def as_dict(self) -> dict[str, dict]:
        return {r["team"]: r for r in self.frame.to_dicts()}


def team_context(season: int | None = None, lookback: int = 3) -> TeamContext:
    """Shrunk pace / pass-rate / defensive-strength estimates for `season`.

    Uses the previous ``lookback`` seasons plus any completed games in `season`
    itself, so it self-updates in-season without any code change.
    """
    season = season or current_season()
    seasons = [s for s in range(season - lookback, season + 1) if s >= 1999]
    tg = team_game_features(seasons)
    tg = tg.filter(pl.col("plays") > 0)
    base = league_baselines(seasons)

    if tg.is_empty():
        teams = nv.load_teams()
        abbr = "team_abbr" if "team_abbr" in teams.columns else "team"
        frame = teams.select(pl.col(abbr).alias("team")).unique().with_columns(
            pl.lit(base["plays"]).alias("exp_plays"),
            pl.lit(base["pass_rate"]).alias("exp_pass_rate"),
            pl.lit(1.0).alias("def_pass_factor"),
            pl.lit(1.0).alias("def_rush_factor"),
            pl.lit(1.0).alias("def_points_factor"),
        )
        return TeamContext(frame, base, season)

    seasons_arr = tg["season"].to_numpy()
    w = _season_weights(seasons_arr, season)
    tg = tg.with_columns(pl.Series("w", w))

    def shrunk(group_col: str, value_col: str, prior: float, prior_games: float, name: str) -> pl.DataFrame:
        g = tg.group_by(group_col).agg(
            (pl.col(value_col) * pl.col("w")).sum().alias("_num"),
            pl.col("w").sum().alias("_den"),
        )
        return g.with_columns(
            ((pl.col("_num") + prior * prior_games) / (pl.col("_den") + prior_games)).alias(name)
        ).select([group_col, name])

    pace = shrunk("team", "plays", base["plays"], PACE_PRIOR_GAMES, "exp_plays")
    prate = shrunk("team", "pass_rate", base["pass_rate"], PASS_RATE_PRIOR_GAMES, "exp_pass_rate")
    pts = shrunk("team", "points_for", base["points"], PACE_PRIOR_GAMES, "exp_points_for")

    # Defence: opponent production allowed relative to league average.
    d_pass = shrunk("opponent_team", "passing_yards", base["passing_yards"], DEF_PRIOR_GAMES, "_dpy")
    d_rush = shrunk("opponent_team", "rushing_yards", base["rushing_yards"], DEF_PRIOR_GAMES, "_dry")
    d_pts = shrunk("opponent_team", "points_for", base["points"], DEF_PRIOR_GAMES, "_dpts")
    d_plays = shrunk("opponent_team", "plays", base["plays"], DEF_PRIOR_GAMES, "_dplays")

    defence = (
        d_pass.join(d_rush, on="opponent_team")
        .join(d_pts, on="opponent_team")
        .join(d_plays, on="opponent_team")
        .rename({"opponent_team": "team"})
        .with_columns(
            (pl.col("_dpy") / base["passing_yards"]).alias("def_pass_factor"),
            (pl.col("_dry") / base["rushing_yards"]).alias("def_rush_factor"),
            (pl.col("_dpts") / base["points"]).alias("def_points_factor"),
            (pl.col("_dplays") / base["plays"]).alias("def_pace_factor"),
        )
        .select(["team", "def_pass_factor", "def_rush_factor", "def_points_factor", "def_pace_factor"])
    )

    frame = (
        pace.join(prate, on="team")
        .join(pts, on="team")
        .join(defence, on="team", how="left")
        .with_columns(
            # Defensive factors are themselves noisy -> pull halfway to neutral.
            *[((pl.col(c).fill_null(1.0) - 1.0) * 0.5 + 1.0).clip(0.82, 1.18).alias(c)
              for c in ("def_pass_factor", "def_rush_factor", "def_points_factor", "def_pace_factor")]
        )
        .sort("team")
    )
    return TeamContext(frame, base, season)


def implied_team_totals(season: int, ctx: TeamContext | None = None) -> pl.DataFrame:
    """Per team-week implied points, plays and pass rate for the season's schedule.

    Vegas lines carry far more information about a week's scoring environment
    than any historical average, so where a line exists it drives the mean and
    the team's own tendencies only shape the split.
    """
    ctx = ctx or team_context(season)
    sched = _reg_season(nv.load_schedules([season]))
    if sched.is_empty():
        return pl.DataFrame()

    home = sched.select(
        pl.col("game_id"), pl.col("week"),
        pl.col("home_team").alias("team"), pl.col("away_team").alias("opponent"),
        pl.col("spread_line").cast(pl.Float64).alias("team_spread"),
        pl.col("total_line").cast(pl.Float64).alias("total_line"),
        pl.lit(1.0).alias("is_home"),
        pl.col("roof"), pl.col("gameday"),
    )
    away = sched.select(
        pl.col("game_id"), pl.col("week"),
        pl.col("away_team").alias("team"), pl.col("home_team").alias("opponent"),
        (-pl.col("spread_line").cast(pl.Float64)).alias("team_spread"),
        pl.col("total_line").cast(pl.Float64).alias("total_line"),
        pl.lit(0.0).alias("is_home"),
        pl.col("roof"), pl.col("gameday"),
    )
    long = pl.concat([home, away])

    base_pts = ctx.baselines["points"]
    base_plays = ctx.baselines["plays"]
    tf = ctx.frame

    long = long.join(tf, on="team", how="left").join(
        tf.select(["team", "def_pass_factor", "def_rush_factor", "def_points_factor", "def_pace_factor"])
        .rename({"team": "opponent", "def_pass_factor": "opp_def_pass_factor",
                 "def_rush_factor": "opp_def_rush_factor", "def_points_factor": "opp_def_points_factor",
                 "def_pace_factor": "opp_def_pace_factor"}),
        on="opponent", how="left",
    )

    # Implied points: Vegas when available, else team strength vs opponent defence.
    model_pts = (pl.col("exp_points_for").fill_null(base_pts) * pl.col("opp_def_points_factor").fill_null(1.0))
    vegas_pts = (pl.col("total_line") / 2.0 + pl.col("team_spread") / 2.0)
    long = long.with_columns(
        pl.when(pl.col("total_line").is_not_null() & pl.col("team_spread").is_not_null())
        .then(0.8 * vegas_pts + 0.2 * model_pts)
        .otherwise(model_pts)
        .clip(6.0, 45.0)
        .alias("implied_points"),
    )

    # Pace: a high-total game is played faster; a big favourite runs more clock.
    long = long.with_columns(
        (
            pl.col("exp_plays").fill_null(base_plays)
            * pl.col("opp_def_pace_factor").fill_null(1.0)
            * (1.0 + 0.004 * (pl.col("total_line").fill_null(ctx.baselines["points"] * 2) - base_pts * 2))
        ).clip(48.0, 80.0).alias("exp_plays_game"),
        # Neutral pass rate: the team's own tendency. The *game-script* response
        # (trailing teams throw) is applied inside the simulator against the
        # realised margin, which is where the signal actually lives -- with team
        # fixed effects the realised margin carries beta ~= -0.0043 per point
        # while the pregame spread adds essentially nothing on top of it.
        pl.col("exp_pass_rate").fill_null(ctx.baselines["pass_rate"]).clip(0.35, 0.78)
        .alias("exp_pass_rate_neutral"),
        (
            pl.col("exp_pass_rate").fill_null(ctx.baselines["pass_rate"])
            - 0.0022 * pl.col("team_spread").fill_null(0.0)
        ).clip(0.35, 0.78).alias("exp_pass_rate_game"),
    )
    long = apply_environment(long)
    return long.sort(["week", "team"])


def apply_environment(long: pl.DataFrame, fetch: bool = True) -> pl.DataFrame:
    """Fold weather into the team-week expectations.

    Two rules, both of which come out of the fit in
    :mod:`gridiron.features.environment`:

    * The **level** adjustment is only what the closing total does not already
      contain. Wind qualifies (-1.83 pts per 10 mph against the line); cold does
      not, and adding it would be counting the same thing twice.
    * The **composition** adjustment applies regardless, because the total line
      prices how many points, not how they are scored. This is where the
      fantasy consequence lives: wind moves touches from the passing game to
      the run, and that reaches individual players through the usage model.
    """
    from ..data.weather import for_game
    from .environment import adjust

    if long.is_empty():
        return long

    home = {}
    for row in long.iter_rows(named=True):
        gid = row.get("game_id")
        if gid and row.get("is_home"):
            home[gid] = row

    cache: dict[str, object] = {}
    for gid, row in home.items():
        kickoff = _kickoff(row.get("gameday"))
        weather = for_game(row.get("team"), kickoff, roof=row.get("roof"), fetch=fetch)
        cache[gid] = (weather, adjust(weather))

    def col(name, pick):
        return pl.Series(name, [
            pick(*cache[r["game_id"]]) if r.get("game_id") in cache else 0.0
            for r in long.iter_rows(named=True)
        ], dtype=pl.Float64)

    long = long.with_columns([
        col("env_total_points", lambda w, a: a.total_points),
        col("env_rush_share", lambda w, a: a.rush_share),
        col("env_comp_pct", lambda w, a: a.comp_pct),
        col("env_ypa", lambda w, a: a.ypa),
        col("env_kick_yards", lambda w, a: a.kick_yards),
        col("env_wind_mph", lambda w, a: w.wind_mph),
        col("env_temp_f", lambda w, a: w.temp_f),
        col("env_elevation_ft", lambda w, a: w.elevation_ft),
        pl.Series("env_indoor", [
            bool(cache[r["game_id"]][0].indoor) if r.get("game_id") in cache else False
            for r in long.iter_rows(named=True)], dtype=pl.Boolean),
        pl.Series("env_reason", [
            cache[r["game_id"]][1].reason if r.get("game_id") in cache else "unknown"
            for r in long.iter_rows(named=True)], dtype=pl.Utf8),
        pl.Series("env_source", [
            cache[r["game_id"]][0].source if r.get("game_id") in cache else "unknown"
            for r in long.iter_rows(named=True)], dtype=pl.Utf8),
    ])

    # Half the game-level effect lands on each side.
    return long.with_columns([
        (pl.col("implied_points") + pl.col("env_total_points") / 2.0)
        .clip(6.0, 45.0).alias("implied_points"),
        (pl.col("exp_pass_rate_neutral") - pl.col("env_rush_share"))
        .clip(0.35, 0.78).alias("exp_pass_rate_neutral"),
        (pl.col("exp_pass_rate_game") - pl.col("env_rush_share"))
        .clip(0.35, 0.78).alias("exp_pass_rate_game"),
    ])


def _kickoff(gameday):
    """Best-effort kickoff time; the hour only has to be close enough to forecast."""
    import datetime as dt

    if gameday is None:
        return None
    if isinstance(gameday, dt.datetime):
        return gameday.replace(tzinfo=gameday.tzinfo or dt.timezone.utc)
    if isinstance(gameday, dt.date):
        return dt.datetime.combine(gameday, dt.time(18), tzinfo=dt.timezone.utc)
    try:
        return dt.datetime.fromisoformat(str(gameday)).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


#: Empirical game-script coefficients, estimated on 2018-2025 team-games with
#: team fixed effects (see ``scripts/calibrate.py``).
GAME_SCRIPT_PASS_BETA = -0.0043   # pass rate per point of realised margin
GAME_SCRIPT_PLAYS_BETA = 0.086    # offensive plays per point of realised margin
PLAYS_RESIDUAL_SD = 8.4           # sd of team plays around its expectation
MARGIN_RESIDUAL_SD = 12.73        # sd of game margin around the closing spread
TOTAL_RESIDUAL_SD = 13.19         # sd of game total around the closing total
