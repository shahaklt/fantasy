import numpy as np
import polars as pl
import pytest

from gridiron.scoring import LeagueSettings, Scoring, score_array, score_expression


def test_presets_differ_on_receptions():
    assert Scoring.preset("ppr").receptions == 1.0
    assert Scoring.preset("half_ppr").receptions == 0.5
    assert Scoring.preset("standard").receptions == 0.0


def test_score_array_matches_hand_calculation():
    s = Scoring.preset("ppr")
    stats = {"receptions": np.array([5.0]), "receiving_yards": np.array([100.0]),
             "receiving_tds": np.array([1.0])}
    # 5 rec + 10 yards points + 6 TD points
    assert score_array(stats, s)[0] == pytest.approx(5 + 10 + 6)


def test_score_expression_matches_score_array():
    s = Scoring.preset("half_ppr")
    df = pl.DataFrame({
        "position": ["WR"], "passing_yards": [0.0], "passing_tds": [0.0],
        "passing_interceptions": [0.0], "rushing_yards": [12.0], "rushing_tds": [0.0],
        "receptions": [7.0], "receiving_yards": [88.0], "receiving_tds": [1.0],
        "rushing_fumbles_lost": [0.0], "receiving_fumbles_lost": [0.0],
        "sack_fumbles_lost": [0.0], "passing_2pt_conversions": [0.0],
        "rushing_2pt_conversions": [0.0], "receiving_2pt_conversions": [0.0],
    })
    from_expr = df.select(score_expression(s))["fantasy_points"][0]
    from_array = score_array({k: np.array([df[k][0]]) for k in
                              ("rushing_yards", "receptions", "receiving_yards", "receiving_tds")}, s)[0]
    assert from_expr == pytest.approx(from_array)


def test_positional_demand_splits_flex():
    league = LeagueSettings(teams=12, roster={"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "BN": 6})
    demand = league.positional_demand()
    assert demand["QB"] == 12
    # 2 RB starters plus a share of the flex
    assert 24 < demand["RB"] < 30
    assert demand["WR"] > demand["RB"]


def test_superflex_lifts_quarterback_demand():
    one_qb = LeagueSettings(teams=12, roster={"QB": 1, "RB": 2, "WR": 3, "TE": 1, "BN": 6})
    superflex = LeagueSettings(teams=12, roster={"QB": 1, "SUPERFLEX": 1, "RB": 2, "WR": 3, "TE": 1, "BN": 6})
    assert superflex.positional_demand()["QB"] > one_qb.positional_demand()["QB"] * 1.5


def test_league_roundtrips_through_dict():
    league = LeagueSettings(teams=10, scoring_preset="ppr", draft_slot=3)
    restored = LeagueSettings.from_dict(league.to_dict())
    assert restored.teams == 10
    assert restored.scoring.receptions == 1.0
    assert restored.draft_slot == 3
