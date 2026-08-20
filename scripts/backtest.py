"""Holdout backtest: fit through season Y-1, predict every game and player-week in Y.

The only test of a projection model that means anything is one where the answer
was genuinely unavailable when the prediction was made. ``GRIDIRON_MAX_SEASON``
caps every history load at the cutoff, so nothing from the holdout season
reaches the usage profiles, the efficiency priors or the fitted constants.

What the holdout season *is* allowed to supply:

* its schedule and closing lines, which are pre-game information and the market
  anchor this model is built on. Removing them would test a different model.
* prior weeks within the season, for the positional matchup factor — week 9's
  prediction may use weeks 1-8, exactly as it would in life.

The market is the benchmark throughout. A model that cannot beat the closing
line is not necessarily useless, but it should say so rather than quietly
report an impressive-looking absolute error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


# ----------------------------------------------------------------------- metrics
def mae(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    m = np.isfinite(a) & np.isfinite(b)
    return float(np.mean(np.abs(a[m] - b[m]))) if m.any() else float("nan")


def rmse(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    m = np.isfinite(a) & np.isfinite(b)
    return float(np.sqrt(np.mean((a[m] - b[m]) ** 2))) if m.any() else float("nan")


def brier(p, y):
    p, y = np.asarray(p, float), np.asarray(y, float)
    m = np.isfinite(p) & np.isfinite(y)
    return float(np.mean((p[m] - y[m]) ** 2)) if m.any() else float("nan")


def log_loss(p, y):
    p, y = np.asarray(p, float), np.asarray(y, float)
    m = np.isfinite(p) & np.isfinite(y)
    p = np.clip(p[m], 1e-6, 1 - 1e-6)
    return float(-np.mean(y[m] * np.log(p) + (1 - y[m]) * np.log(1 - p)))


def spearman(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 3:
        return float("nan")
    ra = np.argsort(np.argsort(a[m])).astype(float)
    rb = np.argsort(np.argsort(b[m])).astype(float)
    return float(np.corrcoef(ra, rb)[0, 1])


def calibration(p, y, bins=10):
    """Predicted vs realised frequency. A model can be sharp and still lie."""
    p, y = np.asarray(p, float), np.asarray(y, float)
    m = np.isfinite(p) & np.isfinite(y)
    p, y = p[m], y[m]
    edges = np.linspace(0, 1, bins + 1)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (p >= lo) & (p < hi) if hi < 1 else (p >= lo) & (p <= hi)
        if sel.sum() < 5:
            continue
        rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": int(sel.sum()),
                     "predicted": round(float(p[sel].mean()), 4),
                     "actual": round(float(y[sel].mean()), 4)})
    ece = sum(r["n"] * abs(r["predicted"] - r["actual"]) for r in rows) / max(len(p), 1)
    return rows, float(ece)


# ------------------------------------------------------------------------ runner
def run(holdout: int, n_sims: int, out_path: Path, max_week: int | None = None) -> dict:
    os.environ["GRIDIRON_MAX_SEASON"] = str(holdout - 1)

    from gridiron import pipeline
    from gridiron.analysis import GameAnalyst
    from gridiron.data.nflverse import load_player_stats, load_schedules
    from gridiron.models.projections import build_projections
    from gridiron.sim.engine import MonteCarloEngine, build_sim_inputs

    print(f"training cutoff: {holdout - 1}   holdout season: {holdout}   sims: {n_sims:,}")
    started = time.perf_counter()

    proj = build_projections(season=holdout, league=pipeline.load_league())
    print(f"projections built: {proj.players.height} players, "
          f"{proj.team_weeks.height} team-weeks  ({time.perf_counter() - started:.0f}s)")

    inputs = build_sim_inputs(proj)
    engine = MonteCarloEngine(inputs, seed=17)
    analyst = GameAnalyst(engine, team_weeks=proj.team_weeks, season=holdout)

    # Scoring needs the truth, which the model's own loaders are now blind to.
    # Lifting the cap for these two reads is the whole point of scoring.
    saved_cutoff = os.environ.pop("GRIDIRON_MAX_SEASON", None)
    actual_games = (load_schedules(holdout)
                    .filter((pl.col("game_type") == "REG") & pl.col("result").is_not_null()))
    actual_players = load_player_stats(holdout)
    if saved_cutoff is not None:
        os.environ["GRIDIRON_MAX_SEASON"] = saved_cutoff
    if "season_type" in actual_players.columns:
        actual_players = actual_players.filter(pl.col("season_type") == "REG")

    weeks = sorted(actual_games["week"].unique().to_list())
    if max_week:
        weeks = [w for w in weeks if w <= max_week]

    game_rows, player_rows = [], []
    for week in weeks:
        try:
            preds, players = analyst.predict_week(week, n_sims=n_sims)
        except Exception as exc:  # noqa: BLE001
            print(f"  week {week:>2}: skipped ({exc})")
            continue

        truth = {(r["home_team"], r["away_team"]): r
                 for r in actual_games.filter(pl.col("week") == week).iter_rows(named=True)}
        for p in preds:
            key = (p.home_team, p.away_team)
            if key not in truth:
                continue
            g = truth[key]
            home_pts, away_pts = float(g["home_score"]), float(g["away_score"])
            game_rows.append({
                "week": week, "home": p.home_team, "away": p.away_team,
                "pred_margin": float(p.margin.mean()), "actual_margin": home_pts - away_pts,
                "pred_total": float(p.total.mean()), "actual_total": home_pts + away_pts,
                "pred_home_win": float((p.margin > 0).mean()),
                "home_won": float(home_pts > away_pts),
                "market_spread": float(g["spread_line"]) if g.get("spread_line") is not None else np.nan,
                "market_total": float(g["total_line"]) if g.get("total_line") is not None else np.nan,
            })

        got = actual_players.filter(pl.col("week") == week)
        if "fantasy_points_ppr" in got.columns and not players.is_empty():
            merged = players.join(
                got.select("player_id", pl.col("fantasy_points_ppr").alias("actual")),
                on="player_id", how="inner")
            for r in merged.iter_rows(named=True):
                player_rows.append({
                    "week": week, "player_id": r["player_id"], "position": r["position"],
                    "pred": float(r["proj_points"]), "actual": float(r["actual"]),
                    "floor": float(r.get("floor") or 0.0), "ceiling": float(r.get("ceiling") or 0.0),
                    "matchup_factor": float(r.get("matchup_factor") or 1.0),
                    # Availability matters for the comparison to be fair: the
                    # projection averages over simulations where the player was
                    # hurt or benched and scored zero, while the actual is only
                    # observed when he played. Dividing by the active share
                    # gives the conditional-on-playing projection, which is the
                    # like-for-like number.
                    "active_pct": float(r.get("active_pct") or 1.0),
                })
        print(f"  week {week:>2}: {len(truth)} games, {got.height} player-weeks")

    games = pl.DataFrame(game_rows)
    plays = pl.DataFrame(player_rows)
    report = summarise(games, plays, holdout, n_sims, time.perf_counter() - started)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    games.write_csv(out_path.with_name(f"backtest_{holdout}_games.csv"))
    if not plays.is_empty():
        plays.write_csv(out_path.with_name(f"backtest_{holdout}_players.csv"))
    return report


def summarise(games: pl.DataFrame, plays: pl.DataFrame, holdout: int,
              n_sims: int, seconds: float) -> dict:
    report: dict = {"holdout_season": holdout, "n_sims": n_sims,
                    "seconds": round(seconds, 1), "games": games.height,
                    "player_weeks": plays.height}

    if not games.is_empty():
        g = games
        # nflverse quotes spread_line as the HOME team's expected margin, so a
        # positive value means the home side is favoured. Verified against
        # 2023-2025: +spread_line gives MAE 9.74 against actual home margin,
        # negating it gives 14.35.
        market_margin = g["market_spread"].to_numpy()
        report["game"] = {
            "margin_mae": mae(g["pred_margin"], g["actual_margin"]),
            "margin_rmse": rmse(g["pred_margin"], g["actual_margin"]),
            "market_margin_mae": mae(market_margin, g["actual_margin"]),
            "total_mae": mae(g["pred_total"], g["actual_total"]),
            "total_rmse": rmse(g["pred_total"], g["actual_total"]),
            "market_total_mae": mae(g["market_total"], g["actual_total"]),
            "win_brier": brier(g["pred_home_win"], g["home_won"]),
            "win_log_loss": log_loss(g["pred_home_win"], g["home_won"]),
            "win_accuracy": float(((g["pred_home_win"].to_numpy() > 0.5).astype(float)
                                   == g["home_won"].to_numpy()).mean()),
            "baseline_brier_always_home": brier(np.full(g.height, g["home_won"].mean()),
                                                g["home_won"]),
            "ats_beat_market": float(np.mean(
                (np.sign(g["pred_margin"].to_numpy() - market_margin)
                 == np.sign(g["actual_margin"].to_numpy() - market_margin))[
                    np.isfinite(market_margin)])),
        }
        rows, ece = calibration(g["pred_home_win"], g["home_won"])
        report["game"]["calibration"] = rows
        report["game"]["calibration_error"] = round(ece, 4)

    if not plays.is_empty():
        report["players"] = {}
        for pos in ["ALL", "QB", "RB", "WR", "TE"]:
            sub = plays if pos == "ALL" else plays.filter(pl.col("position") == pos)
            if sub.height < 20:
                continue
            inside = ((sub["actual"] >= sub["floor"]) & (sub["actual"] <= sub["ceiling"]))
            active = sub["active_pct"].to_numpy().clip(0.2, 1.0)
            conditional = sub["pred"].to_numpy() / active
            report["players"][pos] = {
                "n": sub.height,
                "mae": mae(sub["pred"], sub["actual"]),
                "rmse": rmse(sub["pred"], sub["actual"]),
                "bias": float((sub["pred"] - sub["actual"]).mean()),
                "mae_given_played": mae(conditional, sub["actual"]),
                "bias_given_played": float((conditional - sub["actual"].to_numpy()).mean()),
                "spearman": spearman(sub["pred"], sub["actual"]),
                # The 10th-90th band should contain 80% of outcomes. Anything
                # far off means the distribution is the wrong shape, which
                # matters more than the mean for start/sit decisions.
                "inside_80_band": float(inside.mean()),
            }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2025, help="season to hold out")
    ap.add_argument("--sims", type=int, default=4000)
    ap.add_argument("--max-week", type=int, default=None)
    ap.add_argument("--out", type=Path,
                    default=REPO / "data" / "artifacts" / "backtest.json")
    args = ap.parse_args()

    report = run(args.season, args.sims, args.out, args.max_week)
    print("\n" + json.dumps(report, indent=2)[:4000])
    print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
