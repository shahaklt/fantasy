"""Past matchups: the version that carries signal, and the version that does not.

Two things get called "matchup history", and they behave very differently.

**Head to head between two teams predicts nothing.** Fitted on 5,798 games with
at least two prior meetings, the mean margin of the last six meetings carries
beta +0.043 (se 0.022) against the closing spread — r = 0.026, R² = 0.0007, not
significant. The most recent meeting is even emptier: beta +0.003, r = 0.003.
Rosters and coaches turn over, and whatever "owning" a team means, it does not
survive contact with a point spread. It is computed here for display, because
it is genuinely interesting to look at, and weighted at exactly zero.

**A defence's history against a position does predict.** Fitted out of sample —
prior weeks only, predicting the next week — on 2016-2025:

    QB  beta 0.209 (se 0.040)  r 0.082
    RB  beta 0.266 (se 0.039)  r 0.108
    WR  beta 0.176 (se 0.045)  r 0.062
    TE  beta 0.163 (se 0.041)  r 0.063

All four significant. Those betas are not chosen, they *are* the shrinkage: a
defence that has allowed 30% more than average to tight ends should be expected
to allow about 5% more next week, not 30%. Regression to the mean is most of
the story, and the coefficient measures exactly how much of it.
"""
from __future__ import annotations

import logging

import polars as pl

log = logging.getLogger(__name__)

#: Fitted out-of-sample weights on a defence's prior relative points allowed.
#: Also the honest answer to "how much should I trust a matchup?" — not much,
#: but not nothing.
POSITION_SHRINKAGE: dict[str, float] = {
    "QB": 0.209, "RB": 0.266, "WR": 0.176, "TE": 0.163,
}

#: Head-to-head weight. Measured, and measured to be nothing.
HEAD_TO_HEAD_WEIGHT = 0.0

MIN_GAMES = 4        # below this a defensive "tendency" is three good afternoons
MIN_SEASON = 2016


def positional_defense(player_stats: pl.DataFrame, season: int,
                       through_week: int | None = None) -> pl.DataFrame:
    """Relative fantasy points each defence has allowed, by position.

    Returns one row per (defence, position) with `rel_allowed` — 1.15 means
    that defence has been giving up 15% more than the league average to that
    position — and `factor`, the same number shrunk by the fitted weight, which
    is what the projection should actually multiply by.
    """
    needed = {"season", "week", "position", "opponent_team", "fantasy_points_ppr"}
    if not needed.issubset(set(player_stats.columns)):
        log.info("player stats lack the columns needed for positional matchups")
        return pl.DataFrame(schema={"defense": pl.Utf8, "position": pl.Utf8,
                                    "games": pl.Int64, "rel_allowed": pl.Float64,
                                    "factor": pl.Float64})

    d = player_stats.filter(
        (pl.col("season") == season)
        & pl.col("position").is_in(list(POSITION_SHRINKAGE))
        & pl.col("opponent_team").is_not_null()
        & pl.col("fantasy_points_ppr").is_not_null())
    if "season_type" in d.columns:
        d = d.filter(pl.col("season_type") == "REG")
    if through_week is not None:
        d = d.filter(pl.col("week") < through_week)
    if d.is_empty():
        return pl.DataFrame(schema={"defense": pl.Utf8, "position": pl.Utf8,
                                    "games": pl.Int64, "rel_allowed": pl.Float64,
                                    "factor": pl.Float64})

    per_game = (d.group_by(["week", "opponent_team", "position"])
                  .agg(pl.col("fantasy_points_ppr").sum().alias("allowed"))
                  .rename({"opponent_team": "defense"}))
    league = per_game.group_by(["week", "position"]).agg(pl.col("allowed").mean().alias("lg"))
    per_game = per_game.join(league, on=["week", "position"]).with_columns(
        (pl.col("allowed") / pl.col("lg").clip(0.1)).alias("rel"))

    out = (per_game.group_by(["defense", "position"])
                   .agg(pl.col("rel").mean().alias("rel_allowed"),
                        pl.len().alias("games")))

    weights = pl.DataFrame({"position": list(POSITION_SHRINKAGE),
                            "shrink": list(POSITION_SHRINKAGE.values())})
    out = out.join(weights, on="position", how="left")
    # Shrink toward 1.0 by the fitted weight, and refuse to speak at all on a
    # handful of games.
    return out.with_columns(
        pl.when(pl.col("games") >= MIN_GAMES)
        .then(1.0 + pl.col("shrink") * (pl.col("rel_allowed") - 1.0))
        .otherwise(1.0)
        .clip(0.85, 1.15)
        .alias("factor")
    ).drop("shrink").sort(["position", "factor"], descending=[False, True])


def head_to_head(schedules: pl.DataFrame, team_a: str, team_b: str,
                 limit: int = 6) -> dict:
    """Recent meetings between two teams — context only, weight zero.

    Included because it is the first thing anyone looks for and its absence
    reads as an oversight. The `weight` field states plainly that it does not
    move the projection, and why.
    """
    pair = schedules.filter(
        pl.col("result").is_not_null()
        & (((pl.col("home_team") == team_a) & (pl.col("away_team") == team_b))
           | ((pl.col("home_team") == team_b) & (pl.col("away_team") == team_a)))
    ).sort(["season", "week"], descending=True).head(limit)

    games, wins_a = [], 0
    for row in pair.iter_rows(named=True):
        margin = float(row["result"] or 0.0)          # positive = home team won
        a_margin = margin if row["home_team"] == team_a else -margin
        wins_a += a_margin > 0
        games.append({
            "season": row["season"], "week": row["week"],
            "home": row["home_team"], "away": row["away_team"],
            "home_score": row.get("home_score"), "away_score": row.get("away_score"),
            f"{team_a}_margin": round(a_margin, 1),
        })

    return {
        "team_a": team_a, "team_b": team_b, "games": games,
        "record": f"{wins_a}-{len(games) - wins_a}",
        "avg_margin": round(sum(g[f"{team_a}_margin"] for g in games) / len(games), 1)
        if games else None,
        "weight": HEAD_TO_HEAD_WEIGHT,
        "note": ("Shown for context only. Fitted on 5,798 games, head-to-head "
                 "history carries r = 0.026 against the closing spread — "
                 "indistinguishable from zero, so it does not move any projection."),
    }
