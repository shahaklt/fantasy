"""Compare this model's projections against ESPN's.

Two questions, and they are different:

1. **Where do we disagree, right now?** Useful before games are played. A large
   gap is a candidate to act on — start someone ESPN's league-mates are
   benching, or fade a player the room is over-rating because ESPN says so.
2. **Who was actually right?** Only answerable after the fact, and the only
   question that settles anything. Scored with mean absolute error, RMSE,
   correlation and a head-to-head win rate on the same players and weeks.

The honest framing matters: ESPN's projections drive what the rest of your
league believes, so a disagreement is a market inefficiency in your league even
in the weeks where ESPN turns out to be the more accurate of the two.
"""
from __future__ import annotations

import numpy as np
import polars as pl


def _prep(board: pl.DataFrame) -> pl.DataFrame:
    """Gridiron's board keyed for joining against ESPN rows."""
    from ..data.market import normalize_name

    if board.is_empty():
        return board
    out = board
    if "merge_name" not in out.columns:
        out = out.with_columns(
            pl.col("player_name").map_elements(normalize_name, return_dtype=pl.Utf8)
            .alias("merge_name"))
    keep = [c for c in ("merge_name", "player_id", "player_name", "position", "team",
                        "proj_points", "proj_ppg", "proj_games", "p5", "p95", "proj_sd",
                        "vorp", "tier", "adp") if c in out.columns]
    return out.select(keep)


def season_comparison(board: pl.DataFrame, espn_players: pl.DataFrame,
                      min_points: float = 20.0) -> pl.DataFrame:
    """Season-long projection, side by side, for every player ESPN knows about.

    ESPN publishes a season total and a per-game average. The per-game numbers
    are the fairer comparison, because ESPN's season total assumes a full
    seventeen games while this model prices in expected missed time — comparing
    the totals would score the injury model rather than the projection.
    """
    if board.is_empty() or espn_players.is_empty():
        return pl.DataFrame()

    left = _prep(board)
    right = espn_players.select([c for c in (
        "merge_name", "player_name", "position", "pro_team", "espn_proj_total",
        "espn_proj_avg", "espn_actual_total", "espn_actual_avg", "percent_owned",
        "injured", "fantasy_team") if c in espn_players.columns])
    right = right.unique(subset=["merge_name"], keep="first").rename(
        {"player_name": "espn_name"})
    if "position" in right.columns:
        right = right.rename({"position": "espn_position"})

    df = left.join(right, on="merge_name", how="inner")
    if df.is_empty():
        return df

    df = df.filter((pl.col("proj_points") >= min_points)
                   | (pl.col("espn_proj_total") >= min_points))

    # Per-game is the like-for-like comparison; totals differ by availability.
    df = df.with_columns(
        (pl.col("proj_points") - pl.col("espn_proj_total")).alias("delta_total"),
        (pl.col("proj_ppg") - pl.col("espn_proj_avg")).alias("delta_ppg"),
    ).with_columns(
        (pl.col("delta_ppg") / pl.col("espn_proj_avg").abs().clip(0.5)).alias("delta_pct"),
    ).with_columns(
        pl.when(pl.col("delta_ppg") > 0).then(pl.lit("gridiron higher"))
        .when(pl.col("delta_ppg") < 0).then(pl.lit("espn higher"))
        .otherwise(pl.lit("agree")).alias("direction"),
    )
    return df.sort("delta_ppg", descending=True)


def disagreements(comparison: pl.DataFrame, n: int = 15,
                  min_ppg: float = 4.0) -> dict:
    """The biggest gaps in each direction, restricted to startable players."""
    if comparison.is_empty():
        return {"gridiron_higher": [], "espn_higher": [], "agreement": {}}

    live = comparison.filter(
        (pl.col("proj_ppg") >= min_ppg) | (pl.col("espn_proj_avg") >= min_ppg))
    if live.is_empty():
        live = comparison

    return {
        "gridiron_higher": live.sort("delta_ppg", descending=True).head(n).to_dicts(),
        "espn_higher": live.sort("delta_ppg").head(n).to_dicts(),
        "agreement": agreement_stats(live),
    }


def agreement_stats(comparison: pl.DataFrame) -> dict:
    """How closely the two boards agree, before anyone is proved right."""
    if comparison.is_empty():
        return {}
    a = comparison["proj_ppg"].to_numpy()
    b = comparison["espn_proj_avg"].to_numpy()
    mask = np.isfinite(a) & np.isfinite(b)
    a, b = a[mask], b[mask]
    if len(a) < 3:
        return {"n": int(len(a))}

    rank_a = np.argsort(np.argsort(-a))
    rank_b = np.argsort(np.argsort(-b))
    return {
        "n": int(len(a)),
        "mean_abs_diff_ppg": float(np.mean(np.abs(a - b))),
        "median_abs_diff_ppg": float(np.median(np.abs(a - b))),
        "correlation": float(np.corrcoef(a, b)[0, 1]),
        "rank_correlation": float(np.corrcoef(rank_a, rank_b)[0, 1]),
        "gridiron_higher_pct": float((a > b).mean()),
        "mean_bias_ppg": float(np.mean(a - b)),
        "largest_gap_ppg": float(np.max(np.abs(a - b))),
    }


def weekly_comparison(weekly_board: pl.DataFrame, espn_week: pl.DataFrame,
                      week: int) -> pl.DataFrame:
    """One week: both projections and, if the games are done, the actual result."""
    if weekly_board.is_empty() or espn_week.is_empty():
        return pl.DataFrame()
    from ..data.market import normalize_name

    left = weekly_board
    if "merge_name" not in left.columns:
        left = left.with_columns(
            pl.col("player_name").map_elements(normalize_name, return_dtype=pl.Utf8)
            .alias("merge_name"))
    keep = [c for c in ("merge_name", "player_name", "position", "team", "proj_points",
                        "floor", "ceiling", "boom_pct", "bust_pct") if c in left.columns]
    left = left.select(keep).rename({"proj_points": "gridiron_proj"})

    right = espn_week.select([c for c in (
        "merge_name", "espn_week_proj", "espn_week_actual", "slot", "started",
        "fantasy_team", "on_bye", "pro_opponent") if c in espn_week.columns])
    right = right.unique(subset=["merge_name"], keep="first")

    df = left.join(right, on="merge_name", how="inner")
    if df.is_empty():
        return df

    df = df.with_columns(
        pl.lit(int(week)).alias("week"),
        (pl.col("gridiron_proj") - pl.col("espn_week_proj")).alias("delta"),
    )
    # Actuals only exist once the games have been played.
    played = df.filter(pl.col("espn_week_actual") > 0)
    if not played.is_empty():
        df = df.with_columns(
            (pl.col("gridiron_proj") - pl.col("espn_week_actual")).abs().alias("gridiron_err"),
            (pl.col("espn_week_proj") - pl.col("espn_week_actual")).abs().alias("espn_err"),
        ).with_columns(
            pl.when(pl.col("espn_week_actual") <= 0).then(None)
            .when(pl.col("gridiron_err") < pl.col("espn_err")).then(pl.lit("gridiron"))
            .when(pl.col("espn_err") < pl.col("gridiron_err")).then(pl.lit("espn"))
            .otherwise(pl.lit("tie")).alias("closer"),
        )
    return df.sort("delta", descending=True)


def accuracy_scorecard(weekly: pl.DataFrame) -> dict:
    """Score both projections against what actually happened.

    Only rows where the game has been played count. A projection is judged on
    absolute error, on RMSE (which punishes the big misses that lose weeks) and
    on correlation with the real outcome, which is what tells you whether the
    ordering was right even when the level was not.
    """
    if weekly.is_empty() or "espn_week_actual" not in weekly.columns:
        return {"scored": 0, "note": "no completed games in this range yet"}

    played = weekly.filter(pl.col("espn_week_actual") > 0).drop_nulls(
        ["gridiron_proj", "espn_week_proj", "espn_week_actual"])
    if played.is_empty():
        return {"scored": 0, "note": "no completed games in this range yet"}
    if played.height < 5:
        return {"scored": int(played.height),
                "note": f"only {played.height} completed player-weeks — too few to score"}

    g = played["gridiron_proj"].to_numpy()
    e = played["espn_week_proj"].to_numpy()
    y = played["espn_week_actual"].to_numpy()

    def score(pred):
        err = pred - y
        return {
            "mae": float(np.mean(np.abs(err))),
            "rmse": float(np.sqrt(np.mean(err ** 2))),
            "bias": float(np.mean(err)),
            "correlation": float(np.corrcoef(pred, y)[0, 1]) if np.std(pred) > 0 else 0.0,
        }

    gs, es = score(g), score(e)
    g_closer = np.abs(g - y) < np.abs(e - y)
    e_closer = np.abs(e - y) < np.abs(g - y)
    return {
        "scored": int(len(y)),
        "weeks": sorted({int(w) for w in played["week"].to_list()}) if "week" in played.columns else [],
        "gridiron": gs,
        "espn": es,
        "gridiron_win_rate": float(g_closer.mean()),
        "espn_win_rate": float(e_closer.mean()),
        "ties": float(1.0 - g_closer.mean() - e_closer.mean()),
        "mae_edge": float(es["mae"] - gs["mae"]),
        "verdict": _verdict(gs, es, float(g_closer.mean()), int(len(y))),
    }


def _verdict(gs: dict, es: dict, win_rate: float, n: int) -> str:
    """State the result plainly, including when it is too early to say."""
    edge = es["mae"] - gs["mae"]
    if n < 60:
        return (f"Too early to call on {n} player-weeks. "
                f"Gridiron is closer {win_rate:.0%} of the time so far.")
    if abs(edge) < 0.15:
        return (f"Effectively tied over {n} player-weeks "
                f"(MAE {gs['mae']:.2f} vs {es['mae']:.2f}).")
    better, worse = ("Gridiron", "ESPN") if edge > 0 else ("ESPN", "Gridiron")
    return (f"{better} is more accurate over {n} player-weeks: MAE "
            f"{min(gs['mae'], es['mae']):.2f} vs {max(gs['mae'], es['mae']):.2f}, "
            f"and closer on {max(win_rate, 1 - win_rate):.0%} of players. "
            f"{worse} trails by {abs(edge):.2f} points per player-week.")


def roster_report(my_roster: pl.DataFrame, comparison: pl.DataFrame) -> dict:
    """How the two boards see the players you actually own."""
    if my_roster.is_empty() or comparison.is_empty():
        return {"players": [], "summary": {}}
    names = set(my_roster["merge_name"].to_list())
    mine = comparison.filter(pl.col("merge_name").is_in(list(names)))
    if mine.is_empty():
        return {"players": [], "summary": {"matched": 0, "roster_size": my_roster.height}}
    return {
        "players": mine.sort("proj_ppg", descending=True).to_dicts(),
        "summary": {
            "matched": int(mine.height),
            "roster_size": int(my_roster.height),
            "gridiron_total_ppg": float(mine["proj_ppg"].sum()),
            "espn_total_ppg": float(mine["espn_proj_avg"].sum()),
            "delta_ppg": float(mine["delta_ppg"].sum()),
            "undervalued_by_espn": int((mine["delta_ppg"] > 1.0).sum()),
            "overvalued_by_espn": int((mine["delta_ppg"] < -1.0).sum()),
        },
    }
