"""Game-level predictions with a full individual breakdown.

The simulator already produces coherent games -- one margin and one total per
matchup, with every player's line drawn inside it. This module reads those
simulations back out at three levels:

1. **Game** -- score distribution, win/cover/over probabilities, and how they
   compare with the market's own line.
2. **Team** -- the projected points decomposed into passing, rushing and
   receiving production plus touchdowns and field goals.
3. **Player** -- each player's contribution to his team's total, with the
   distribution rather than a single number.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import polars as pl

from ..quant.game_model import GameLine, price_game
from ..sim.engine import MonteCarloEngine, SimInputs


@dataclass
class GamePrediction:
    game_id: str
    week: int
    home_team: str
    away_team: str
    market_spread: float
    market_total: float
    home_points: np.ndarray = field(repr=False)
    away_points: np.ndarray = field(repr=False)

    # ------------------------------------------------------------- summaries
    @property
    def margin(self) -> np.ndarray:
        return self.home_points - self.away_points

    @property
    def total(self) -> np.ndarray:
        return self.home_points + self.away_points

    def summary(self) -> dict:
        margin, total = self.margin, self.total
        return {
            "game_id": self.game_id,
            "week": self.week,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "market_spread": self.market_spread,
            "market_total": self.market_total,
            "proj_home_score": float(self.home_points.mean()),
            "proj_away_score": float(self.away_points.mean()),
            "proj_margin": float(margin.mean()),
            "proj_total": float(total.mean()),
            "margin_sd": float(margin.std()),
            "total_sd": float(total.std()),
            "home_win_prob": float((margin > 0).mean() + 0.5 * (margin == 0).mean()),
            "away_win_prob": float((margin < 0).mean() + 0.5 * (margin == 0).mean()),
            "home_cover_prob": float((margin > self.market_spread).mean()),
            "over_prob": float((total > self.market_total).mean()),
            "spread_edge": float(margin.mean() - self.market_spread),
            "total_edge": float(total.mean() - self.market_total),
            "home_score_p10": float(np.percentile(self.home_points, 10)),
            "home_score_p90": float(np.percentile(self.home_points, 90)),
            "away_score_p10": float(np.percentile(self.away_points, 10)),
            "away_score_p90": float(np.percentile(self.away_points, 90)),
        }

    def score_distribution(self, bins: int = 12) -> dict:
        """Histogram of the simulated total and margin, for plotting."""
        t_hist, t_edges = np.histogram(self.total, bins=bins)
        m_hist, m_edges = np.histogram(self.margin, bins=bins)
        return {
            "total": {"counts": t_hist.tolist(), "edges": t_edges.tolist()},
            "margin": {"counts": m_hist.tolist(), "edges": m_edges.tolist()},
        }

    def alt_lines(self, spreads: list[float] | None = None,
                  totals: list[float] | None = None) -> dict:
        """Probabilities across a ladder of alternate spreads and totals."""
        margin, total = self.margin, self.total
        spreads = spreads or [self.market_spread + d for d in (-7, -3.5, -1.5, 0, 1.5, 3.5, 7)]
        totals = totals or [self.market_total + d for d in (-7, -3.5, 0, 3.5, 7)]
        return {
            "spreads": [{"line": float(s), "home_cover": float((margin > s).mean())}
                        for s in spreads],
            "totals": [{"line": float(t), "over": float((total > t).mean())}
                       for t in totals],
            "team_totals": {
                self.home_team: [{"line": float(l), "over": float((self.home_points > l).mean())}
                                 for l in self._team_ladder(self.home_points)],
                self.away_team: [{"line": float(l), "over": float((self.away_points > l).mean())}
                                 for l in self._team_ladder(self.away_points)],
            },
        }

    @staticmethod
    def _team_ladder(points: np.ndarray) -> list[float]:
        base = round(float(points.mean()) * 2) / 2
        return [base + d for d in (-7, -3.5, 0, 3.5, 7)]

    def analytic_comparison(self) -> dict:
        """The same game priced by the closed-form Stern model, as a cross-check."""
        line = GameLine(self.home_team, self.away_team, self.market_spread, self.market_total)
        return price_game(line)


class GameAnalyst:
    """Runs the simulator for a week and reads out game-level predictions."""

    def __init__(self, engine: MonteCarloEngine):
        self.engine = engine
        self.inp: SimInputs = engine.inp

    def week_index(self, week: int) -> int:
        weeks = list(self.inp.weeks)
        return weeks.index(week) if week in weeks else 0

    def predict_week(self, week: int, n_sims: int = 20_000,
                     with_players: bool = True) -> tuple[list[GamePrediction], pl.DataFrame]:
        """Simulate one week and return game predictions plus player detail."""
        wi = self.week_index(week)
        res = self.engine.simulate_week(wi, n_sims, collect_stats=with_players)

        t_index = {t: i for i, t in enumerate(self.inp.team_list)}
        preds: list[GamePrediction] = []
        for gi, (h, a) in enumerate(zip(self.inp.games_home[wi], self.inp.games_away[wi])):
            preds.append(GamePrediction(
                game_id=self.inp.games_id[wi][gi] if gi < len(self.inp.games_id[wi]) else "",
                week=week,
                home_team=self.inp.team_list[h],
                away_team=self.inp.team_list[a],
                market_spread=float(self.inp.games_spread[wi][gi]),
                market_total=float(self.inp.games_total[wi][gi]),
                home_points=res.team_points[:, h],
                away_points=res.team_points[:, a],
            ))

        players = self._player_frame(res, week) if with_players else pl.DataFrame()
        return preds, players

    def _player_frame(self, res, week: int) -> pl.DataFrame:
        inp = self.inp
        pts = res.points
        data = {
            "week": [week] * inp.n_players,
            "player_id": inp.player_ids,
            "player_name": inp.player_names,
            "position": list(inp.positions),
            "team": inp.teams,
            "proj_points": pts.mean(axis=0),
            "floor": np.percentile(pts, 10, axis=0),
            "median": np.percentile(pts, 50, axis=0),
            "ceiling": np.percentile(pts, 90, axis=0),
            "sd": pts.std(axis=0),
            "boom_pct": (pts >= 20).mean(axis=0),
            "bust_pct": (pts <= 5).mean(axis=0),
            "active_pct": res.active.mean(axis=0),
        }
        for name, arr in res.stats.items():
            data[name] = arr.mean(axis=0)
        return pl.DataFrame(data).filter(pl.col("proj_points") > 0.05).sort(
            "proj_points", descending=True)

    def team_breakdown(self, players: pl.DataFrame, team: str) -> dict:
        """How a team's projected points decompose across its players."""
        sub = players.filter(pl.col("team") == team).sort("proj_points", descending=True)
        if sub.is_empty():
            return {"team": team, "players": [], "totals": {}}

        def col(name: str) -> float:
            return float(sub[name].sum()) if name in sub.columns else 0.0

        pass_yards = col("passing_yards")
        rush_yards = col("rushing_yards")
        rec_yards = col("receiving_yards")
        pass_tds = col("passing_tds")
        rush_tds = col("rushing_tds")
        return {
            "team": team,
            "totals": {
                "fantasy_points": col("proj_points"),
                "passing_yards": pass_yards,
                "rushing_yards": rush_yards,
                "receiving_yards": rec_yards,
                "passing_tds": pass_tds,
                "rushing_tds": rush_tds,
                "total_tds": pass_tds + rush_tds,
                "targets": col("targets"),
                "carries": col("carries"),
                "receptions": col("receptions"),
            },
            "players": sub.head(14).to_dicts(),
        }

    def star_players(self, players: pl.DataFrame, top_n: int = 20) -> pl.DataFrame:
        """Who is most likely to have a big week, not merely a good average.

        Ranked by ceiling rather than mean: in weekly fantasy and in player
        markets, the players worth acting on are the ones with real upside, and
        a high floor with no ceiling is a different (and less useful) profile.
        """
        if players.is_empty():
            return players
        return (
            players.with_columns(
                (pl.col("ceiling") - pl.col("proj_points")).alias("upside"),
                (pl.col("boom_pct") * 100).alias("boom_pct_display"),
            )
            .sort(["boom_pct", "ceiling"], descending=[True, True])
            .head(top_n)
        )
