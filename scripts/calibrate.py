"""Refit the simulator's constants from nflverse data.

Writes ``data/artifacts/calibration.json``, which overrides the committed
defaults in ``gridiron.sim.constants`` at import time. Re-run it after a season
finishes and the engine picks up the new numbers with no code change.

    python scripts/calibrate.py            # fit and write
    python scripts/calibrate.py --dry-run  # fit and print only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np
import polars as pl

from gridiron.config import current_season
from gridiron.data import nflverse as nv
from gridiron.features.players import player_week_features
from gridiron.features.team import team_game_features
from gridiron.sim.constants import CALIBRATION_PATH, Calibration


def fit_game_outcomes(seasons: list[int]) -> dict:
    """Residual dispersion of margin and total around the closing line."""
    s = nv.load_schedules(seasons).filter(pl.col("game_type") == "REG")
    s = s.drop_nulls(["home_score", "away_score", "spread_line", "total_line"])
    margin = (s["home_score"] - s["away_score"]).cast(pl.Float64).to_numpy()
    total = (s["home_score"] + s["away_score"]).cast(pl.Float64).to_numpy()
    spread = s["spread_line"].cast(pl.Float64).to_numpy()
    line = s["total_line"].cast(pl.Float64).to_numpy()
    rm, rt = margin - spread, total - line
    return {
        "margin_sd": float(rm.std()),
        "total_sd": float(rt.std()),
        "margin_total_corr": float(np.corrcoef(rm, rt)[0, 1]),
        "_n_games": int(len(margin)),
    }


def fit_team_volume(seasons: list[int]) -> dict:
    """Pace, pass rate and their response to the realised margin (team fixed effects)."""
    tg = team_game_features(seasons).drop_nulls(["pass_rate", "plays", "points_for", "points_against"])
    tg = tg.with_columns((pl.col("points_for") - pl.col("points_against")).alias("margin"))
    cols = ["pass_rate", "plays", "margin"]
    tg = tg.with_columns([(pl.col(c) - pl.col(c).mean().over("team")).alias(f"{c}_c") for c in cols])

    x = tg["margin_c"].to_numpy()
    design = np.c_[np.ones_like(x), x]
    y_pr = tg["pass_rate_c"].to_numpy()
    y_pl = tg["plays_c"].to_numpy()
    pr = np.linalg.lstsq(design, y_pr, rcond=None)[0]
    pl_ = np.linalg.lstsq(design, y_pl, rcond=None)[0]
    # The sd the simulator needs is the *residual* after the game-script term,
    # not the total spread -- the margin effect is applied separately, and
    # using the raw sd would double-count it.
    resid_pr = y_pr - design @ pr
    resid_pl = y_pl - design @ pl_
    return {
        "pass_rate_per_margin": float(pr[1]),
        "plays_per_margin": float(pl_[1]),
        "plays_sd": float(resid_pl.std()),
        "pass_rate_sd": float(resid_pr.std()),
        "_n_team_games": int(tg.height),
    }


def fit_scoring(seasons: list[int]) -> dict:
    """Offensive touchdowns as a function of team points, and the pass/rush split."""
    ts = nv.load_team_stats(seasons).filter(pl.col("season_type") == "REG")
    sched = nv.load_schedules(seasons).filter(pl.col("game_type") == "REG")
    home = sched.select(["season", "week", pl.col("home_team").alias("team"),
                         pl.col("home_score").alias("pts")])
    away = sched.select(["season", "week", pl.col("away_team").alias("team"),
                         pl.col("away_score").alias("pts")])
    games = pl.concat([home, away])
    j = ts.select(["season", "week", "team", "passing_tds", "rushing_tds"]).join(
        games, on=["season", "week", "team"])
    x = j["pts"].cast(pl.Float64).to_numpy()
    ptd = j["passing_tds"].cast(pl.Float64).to_numpy()
    rtd = j["rushing_tds"].cast(pl.Float64).to_numpy()
    y = ptd + rtd
    b = np.linalg.lstsq(np.c_[np.ones_like(x), x], y, rcond=None)[0]
    resid = y - (b[0] + b[1] * x)

    season_split = j.group_by(["season", "team"]).agg(
        pl.col("passing_tds").cast(pl.Float64).sum().alias("p"),
        pl.col("rushing_tds").cast(pl.Float64).sum().alias("r"))
    share = (season_split["p"] / (season_split["p"] + season_split["r"])).to_numpy()
    n_td = float((season_split["p"] + season_split["r"]).mean())
    binom_var = 0.615 * 0.385 / max(n_td, 1)
    true_var = max(share.var() - binom_var, 1e-6)
    logit_sd = float(np.std(np.log(share / (1 - share))) * np.sqrt(true_var) / max(share.std(), 1e-9))
    return {
        "td_intercept": float(b[0]),
        "td_slope": float(b[1]),
        "td_sd": float(resid.std()),
        "pass_td_share": float(ptd.sum() / max(y.sum(), 1)),
        "pass_td_share_logit_sd": float(np.clip(logit_sd, 0.05, 0.6)),
    }


def fit_yardage(seasons: list[int]) -> dict:
    """Shifted-gamma parameters for per-play receiving and rushing yards."""
    pbp = nv.load_pbp(seasons).filter(pl.col("season_type") == "REG")
    rec = pbp.filter(pl.col("complete_pass") == 1)["receiving_yards"].cast(
        pl.Float64).drop_nulls().to_numpy()
    rush = pbp.filter(pl.col("rush_attempt") == 1)["rushing_yards"].cast(
        pl.Float64).drop_nulls().to_numpy()

    def shape_for(y: np.ndarray, shift: float) -> float:
        z = y + shift
        z = z[z > 0]
        return float(z.mean() ** 2 / z.var())

    db = pbp.filter((pl.col("pass_attempt") == 1) | (pl.col("sack") == 1))
    sack_rate = float(db["sack"].cast(pl.Float64).mean())
    return {
        "rec_yards_shape": shape_for(rec, 3.0),
        "rec_yards_shift": 3.0,
        "rush_yards_shape": shape_for(rush, 3.0),
        "rush_yards_shift": 3.0,
        "league_sack_rate": sack_rate,
        "_n_plays": int(len(rec) + len(rush)),
    }


def fit_usage_dispersion(seasons: list[int]) -> dict:
    """Dirichlet concentration implied by week-to-week share variance."""
    pw = player_week_features(seasons)
    g = pw.group_by(["player_id", "season"]).agg(
        pl.len().alias("n"),
        pl.col("target_share_calc").mean().alias("tm"), pl.col("target_share_calc").std().alias("ts"),
        pl.col("carry_share").mean().alias("cm"), pl.col("carry_share").std().alias("cs"))

    def kappa(frame, mean_col, sd_col, floor) -> float:
        sub = frame.filter((pl.col("n") >= 12) & (pl.col(mean_col) > floor)).drop_nulls([sd_col])
        if sub.is_empty():
            return float("nan")
        m = sub[mean_col].to_numpy()
        s = sub[sd_col].to_numpy()
        k = m * (1 - m) / np.maximum(s ** 2, 1e-9) - 1
        return float(np.median(k[np.isfinite(k)]))

    return {
        "target_share_kappa": kappa(g, "tm", "ts", 0.10),
        "carry_share_kappa": kappa(g, "cm", "cs", 0.15),
    }


def fit_team_strength(seasons: list[int]) -> dict:
    """How wrong a preseason view of a team's scoring is, over a whole season."""
    tg = team_game_features(seasons).drop_nulls(["points_for"])
    s = tg.group_by(["season", "team"]).agg(pl.col("points_for").mean().alias("ppg"))
    prev = s.with_columns((pl.col("season") + 1).alias("nseason")).rename(
        {"ppg": "prev"}).select(["nseason", "team", "prev"])
    j = s.join(prev, left_on=["season", "team"], right_on=["nseason", "team"])
    if j.height < 30:
        return {}
    r = float(np.corrcoef(j["ppg"], j["prev"])[0, 1])
    sd = float(s["ppg"].std())
    unpredictable = sd * np.sqrt(max(1 - r ** 2, 0.0))
    sampling = 10.3 / np.sqrt(17)
    # The market forecasts better than last season alone, so shade the residual.
    return {"team_strength_sd": float(np.clip(
        np.sqrt(max(unpredictable ** 2 - sampling ** 2, 0.0)) * 0.85, 1.0, 5.0))}


def fit_kicking(seasons: list[int]) -> dict:
    ps = nv.load_player_stats(seasons).filter(
        (pl.col("season_type") == "REG") & (pl.col("position") == "K"))
    agg = ps.group_by(["season", "week", "team"]).agg(
        pl.col("fg_att").cast(pl.Float64).sum().alias("fg_att"),
        pl.col("pat_att").cast(pl.Float64).sum().alias("pat_att"),
        pl.col("pat_made").cast(pl.Float64).sum().alias("pat_made"))
    sched = nv.load_schedules(seasons).filter(pl.col("game_type") == "REG")
    games = pl.concat([
        sched.select(["season", "week", pl.col("home_team").alias("team"), pl.col("home_score").alias("pts")]),
        sched.select(["season", "week", pl.col("away_team").alias("team"), pl.col("away_score").alias("pts")])])
    j = agg.with_columns(pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64)).join(
        games.with_columns(pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64)),
        on=["season", "week", "team"])
    x = j["pts"].cast(pl.Float64).to_numpy()
    b = np.linalg.lstsq(np.c_[np.ones_like(x), x], j["fg_att"].to_numpy(), rcond=None)[0]
    return {
        "fg_att_intercept": float(b[0]),
        "fg_att_slope": float(b[1]),
        "pat_rate": float(j["pat_made"].sum() / max(j["pat_att"].sum(), 1)),
    }


def main(lookback: int = 3, write: bool = True) -> dict:
    season = current_season()
    recent = [s for s in range(season - lookback, season) if s >= 1999]
    long = [s for s in range(season - 8, season) if s >= 1999]

    fitted: dict = {}
    steps = [
        ("game outcomes", lambda: fit_game_outcomes(long)),
        ("team volume", lambda: fit_team_volume(long)),
        ("scoring", lambda: fit_scoring(recent + [season - lookback - 1])),
        ("yardage", lambda: fit_yardage(recent)),
        ("usage dispersion", lambda: fit_usage_dispersion(recent)),
        ("team strength", lambda: fit_team_strength(long)),
        ("kicking", lambda: fit_kicking(recent)),
    ]
    for name, fn in steps:
        try:
            out = fn()
            fitted.update({k: v for k, v in out.items() if not k.startswith("_")})
            notes = {k: v for k, v in out.items() if k.startswith("_")}
            print(f"  {name:<20} ok  {notes if notes else ''}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:<20} [failed] {exc}")

    defaults = Calibration()
    print("\n  parameter                    fitted     default     change")
    for k, v in sorted(fitted.items()):
        if not isinstance(v, (int, float)) or not np.isfinite(v):
            continue
        d = getattr(defaults, k, None)
        if d is None:
            continue
        change = "" if d == 0 else f"{(v / d - 1) * 100:+7.1f}%"
        print(f"  {k:<28} {v:>8.4f}  {d:>8.4f}   {change}")

    clean = {k: float(v) for k, v in fitted.items()
             if isinstance(v, (int, float)) and np.isfinite(v) and hasattr(defaults, k)}
    if write:
        CALIBRATION_PATH.write_text(json.dumps(clean, indent=2, sort_keys=True))
        print(f"\nwrote {CALIBRATION_PATH}")
    return clean


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    main(lookback=args.seasons, write=not args.dry_run)
