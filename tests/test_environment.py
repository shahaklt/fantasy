"""Weather, venue and matchup effects.

The coefficients are fitted, so these tests do not re-check the arithmetic of
the fit. They pin the *decisions* — which effects apply, which deliberately do
not, and what happens when a source cannot answer. Those are the things a
future change could silently get wrong.
"""
from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from gridiron.data import weather as wx
from gridiron.data.stadiums import BY_TEAM, air_density_ratio, lookup
from gridiron.features.environment import EnvironmentEffects, adjust, describe
from gridiron.features.matchup import (HEAD_TO_HEAD_WEIGHT, POSITION_SHRINKAGE,
                                       head_to_head, positional_defense)


# ------------------------------------------------------------------- stadiums
def test_every_team_has_a_venue():
    assert len(BY_TEAM) == 32
    assert BY_TEAM["DEN"].elevation_ft == 5280
    assert BY_TEAM["LA"].lat == BY_TEAM["LAC"].lat, "SoFi is one building"


def test_air_thins_with_height():
    assert air_density_ratio(0) == pytest.approx(1.0)
    assert air_density_ratio(5280) < 0.83
    assert air_density_ratio(2030) < air_density_ratio(600)


def test_a_neutral_site_resolves_by_name():
    assert lookup(stadium_name="Estadio Azteca, Mexico City").elevation_ft > 7000


# -------------------------------------------------------------------- weather
def test_a_closed_roof_is_known_not_fetched():
    w = wx.for_game("NO", roof="closed")
    assert w.indoor and w.source == "indoor" and w.wind_mph == 0.0


def test_a_retractable_roof_follows_the_game_not_the_building():
    """Teams decide on the day, so only the game row can say."""
    assert wx.for_game("DAL", roof="closed").indoor
    assert not wx.for_game("DAL", roof="outdoors").indoor


def test_recorded_conditions_beat_a_forecast():
    w = wx.for_game("BUF", roof="outdoors", temp=18, wind=24)
    assert w.source == "historical" and w.temp_f == 18 and w.wind_mph == 24


def test_an_unreachable_forecast_is_labelled_not_invented():
    w = wx.for_game("GB", kickoff=datetime(2030, 1, 1, tzinfo=timezone.utc),
                    roof="outdoors", fetch=False)
    assert w.source == "default", "a default must never look like a measurement"
    assert adjust(w).is_neutral, "and it must not move a projection"


# -------------------------------------------------------- environment effects
def test_indoors_is_exactly_neutral():
    adj = adjust(wx.GameWeather(70, 0, indoor=True, source="indoor"))
    assert adj.is_neutral and "Indoors" in describe(adj)


def test_wind_moves_the_total_because_the_market_does_not_price_it():
    adj = adjust(wx.GameWeather(60, 26, source="forecast"))
    assert adj.total_points < -2.0, "measured at -1.83 points per 10 mph"
    assert adj.rush_share > 0, "and the offense leans on the run"
    assert adj.comp_pct < 0
    assert adj.kick_yards < -2.0


def test_cold_moves_the_mix_but_never_the_total():
    """The level effect of temperature is already in the closing line.

    Fitted at -0.07 points per ten degrees against the market — noise. Adding
    it would count the same thing twice.
    """
    adj = adjust(wx.GameWeather(5, 6, source="forecast"))
    assert adj.total_points == pytest.approx(0.0), "temperature must not touch the total"
    assert adj.rush_share > 0.01, "but it clearly moves the pass/rush split"
    assert adj.comp_pct < 0


def test_there_is_no_altitude_kicking_knob():
    """Fitted on 12,787 field goals with distance controlled: -0.01 yd per 1000 ft.

    Indistinguishable from zero. The physics is real and the effect is below
    the noise floor, so no adjustment is applied.
    """
    assert EnvironmentEffects().altitude_kick_yards_per_1000ft == 0.0
    thin = adjust(wx.GameWeather(60, 6, elevation_ft=5280, source="forecast"))
    sea = adjust(wx.GameWeather(60, 6, elevation_ft=0, source="forecast"))
    assert thin.kick_yards == pytest.approx(sea.kick_yards)


def test_a_hurricane_does_not_extrapolate_forever():
    capped = adjust(wx.GameWeather(60, 80, source="forecast"))
    at_limit = adjust(wx.GameWeather(60, EnvironmentEffects().max_wind_mph, source="forecast"))
    assert capped.total_points == pytest.approx(at_limit.total_points)


# -------------------------------------------------------------------- matchup
def _stats():
    rows = []
    for week in range(1, 9):
        for team, mult in (("AAA", 1.6), ("BBB", 0.5), ("CCC", 1.0)):
            rows.append({"season": 2025, "week": week, "season_type": "REG",
                         "position": "TE", "opponent_team": team,
                         "fantasy_points_ppr": 10.0 * mult})
    return pl.DataFrame(rows)


def test_a_soft_defence_is_shrunk_toward_average():
    f = positional_defense(_stats(), season=2025)
    soft = f.filter((pl.col("defense") == "AAA") & (pl.col("position") == "TE")).row(0, named=True)
    assert soft["rel_allowed"] > 1.4
    # 0.163 of the way from 1.0, not the whole distance.
    assert 1.05 < soft["factor"] < 1.12, "regression to the mean is most of the story"


def test_a_defence_with_too_little_history_says_nothing():
    thin = _stats().filter(pl.col("week") <= 2)
    f = positional_defense(thin, season=2025)
    assert (f["factor"] == 1.0).all(), "three good afternoons is not a tendency"


def test_the_shrinkage_weights_are_the_fitted_ones():
    assert POSITION_SHRINKAGE["RB"] > POSITION_SHRINKAGE["TE"]
    assert all(0.1 < w < 0.35 for w in POSITION_SHRINKAGE.values())


def test_head_to_head_is_context_and_carries_no_weight():
    sched = pl.DataFrame([{"season": 2024, "week": 5, "home_team": "KC", "away_team": "BUF",
                           "home_score": 30, "away_score": 20, "result": 10.0}])
    h = head_to_head(sched, "KC", "BUF")
    assert h["record"] == "1-0" and h["avg_margin"] == 10.0
    assert h["weight"] == HEAD_TO_HEAD_WEIGHT == 0.0
    assert "0.026" in h["note"], "the measurement should be visible, not just the verdict"
