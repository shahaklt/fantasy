"""ESPN adapter and comparison tests.

ESPN's endpoints cannot be reached from a test, so the adapter is exercised
against the espn-api project's own recorded fixtures — real ESPN payloads —
and the comparison maths is tested on constructed frames where the right
answer is known by hand.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from gridiron.analysis.compare import (accuracy_scorecard, agreement_stats,
                                       disagreements, roster_report,
                                       season_comparison, weekly_comparison)
from gridiron.data.espn import (POSITION_FIX, TEAM_FIX, EspnCredentials, EspnLeague,
                                _owner_names, _player_row, _swid, espn_available)

FIXTURES = Path("/workspace/cwendt94/espn-api/tests/football/unit/data")


class TestCredentials:
    def test_private_needs_both_cookies(self):
        assert EspnCredentials(1, "s2", "swid").is_private
        assert not EspnCredentials(1, "s2", "").is_private
        assert not EspnCredentials(1).is_private

    def test_swid_gets_braces_either_way(self):
        assert _swid("ABC-123") == "{ABC-123}"
        assert _swid("{ABC-123}") == "{ABC-123}"
        assert _swid("") == ""

    def test_env_wins_over_file(self, monkeypatch):
        monkeypatch.setenv("ESPN_LEAGUE_ID", "998877")
        monkeypatch.setenv("ESPN_S2", "cookie")
        monkeypatch.setenv("ESPN_SWID", "swid")
        creds = EspnCredentials.load()
        assert creds.league_id == 998877 and creds.is_private

    def test_missing_config_is_a_clear_error(self, monkeypatch, tmp_path):
        import gridiron.data.espn as espn_mod
        monkeypatch.delenv("ESPN_LEAGUE_ID", raising=False)
        monkeypatch.setattr(espn_mod, "CREDENTIALS_PATH", tmp_path / "none.json")
        with pytest.raises(RuntimeError, match="No ESPN league configured"):
            EspnLeague()

    def test_saved_credentials_are_not_world_readable(self, monkeypatch, tmp_path):
        import gridiron.data.espn as espn_mod
        path = tmp_path / "espn_credentials.json"
        monkeypatch.setattr(espn_mod, "CREDENTIALS_PATH", path)
        EspnCredentials(123, "s2", "{swid}").save()
        assert json.loads(path.read_text())["league_id"] == 123
        assert path.stat().st_mode & 0o077 == 0, "cookies must not be group/world readable"


class TestNormalisation:
    def test_espn_position_names_are_mapped(self):
        assert POSITION_FIX["D/ST"] == "DST"
        assert POSITION_FIX["FB"] == "RB"

    def test_pro_team_abbreviations_match_nflverse(self):
        assert TEAM_FIX["WSH"] == "WAS"
        assert TEAM_FIX["JAC"] == "JAX"
        assert TEAM_FIX["LAR"] == "LA"

    def test_owners_handle_both_shapes(self):
        class T:
            owners = [{"firstName": "Sam", "lastName": "Jones"}]
        assert _owner_names(T()) == ["Sam Jones"]

        class T2:
            owners = ["Plain String"]
        assert _owner_names(T2()) == ["Plain String"]

        class T3:
            owners = None
        assert _owner_names(T3()) == []

    def test_player_row_survives_missing_attributes(self):
        class Bare:
            name = "A.J. Brown"
        row = _player_row(Bare())
        assert row["player_name"] == "A.J. Brown"
        assert row["merge_name"] == "ajbrown"       # joins onto our board
        assert row["espn_proj_total"] == 0.0
        assert row["injured"] is False


@pytest.mark.skipif(not FIXTURES.exists(), reason="espn-api fixtures not checked out")
class TestAgainstRealPayloads:
    """Parse genuine ESPN responses recorded by the library's own test suite."""

    def test_player_payload_parses_into_our_shape(self):
        raw = json.loads((FIXTURES / "league_2018_data.json").read_text())
        from espn_api.football.player import Player

        entries = []
        for team in raw.get("teams", []):
            for entry in (team.get("roster", {}) or {}).get("entries", []):
                entries.append(entry)
        assert entries, "fixture should contain rostered players"

        rows = [_player_row(Player(e, year=2018)) for e in entries[:40]]
        assert all(r["player_name"] for r in rows)
        assert all(r["merge_name"] == r["merge_name"].lower() for r in rows)
        assert {r["position"] for r in rows} <= {"QB", "RB", "WR", "TE", "K", "DST", ""}
        # Projections should be numeric, not strings or None.
        assert all(isinstance(r["espn_proj_total"], float) for r in rows)

    def test_positions_come_through_the_map(self):
        raw = json.loads((FIXTURES / "league_2018_data.json").read_text())
        from espn_api.football.player import Player

        seen = set()
        for team in raw.get("teams", []):
            for entry in (team.get("roster", {}) or {}).get("entries", []):
                seen.add(_player_row(Player(entry, year=2018))["position"])
        assert "DST" not in seen or "D/ST" not in seen, "D/ST must be normalised"
        assert {"QB", "RB", "WR", "TE"} & seen


class TestSeasonComparison:
    @pytest.fixture
    def board(self):
        return pl.DataFrame({
            "player_name": ["Ja'Marr Chase", "Bijan Robinson", "Travis Kelce", "Nobody Here"],
            "merge_name": ["jamarrchase", "bijanrobinson", "traviskelce", "nobodyhere"],
            "position": ["WR", "RB", "TE", "WR"],
            "team": ["CIN", "ATL", "KC", "FA"],
            "proj_points": [300.0, 260.0, 180.0, 10.0],
            "proj_ppg": [19.0, 16.0, 11.0, 1.0],
            "proj_games": [15.8, 16.2, 16.0, 10.0],
        })

    @pytest.fixture
    def espn(self):
        return pl.DataFrame({
            "merge_name": ["jamarrchase", "bijanrobinson", "traviskelce", "someoneelse"],
            "player_name": ["Ja'Marr Chase", "Bijan Robinson", "Travis Kelce", "Someone Else"],
            "position": ["WR", "RB", "TE", "QB"],
            "pro_team": ["CIN", "ATL", "KC", "NYJ"],
            "espn_proj_total": [280.0, 275.0, 190.0, 200.0],
            "espn_proj_avg": [16.5, 17.2, 11.9, 12.5],
            "espn_actual_total": [0.0, 0.0, 0.0, 0.0],
            "espn_actual_avg": [0.0, 0.0, 0.0, 0.0],
            "percent_owned": [99.0, 99.0, 95.0, 60.0],
            "injured": [False, False, False, False],
            "fantasy_team": ["A", "B", "C", "D"],
        })

    def test_only_players_on_both_boards_are_compared(self, board, espn):
        out = season_comparison(board, espn, min_points=0)
        assert set(out["merge_name"].to_list()) == {"jamarrchase", "bijanrobinson", "traviskelce"}

    def test_delta_is_signed_from_our_point_of_view(self, board, espn):
        out = season_comparison(board, espn, min_points=0)
        chase = out.filter(pl.col("merge_name") == "jamarrchase").to_dicts()[0]
        assert chase["delta_ppg"] == pytest.approx(19.0 - 16.5)
        assert chase["direction"] == "gridiron higher"
        bijan = out.filter(pl.col("merge_name") == "bijanrobinson").to_dicts()[0]
        assert bijan["direction"] == "espn higher"

    def test_sorted_by_disagreement(self, board, espn):
        out = season_comparison(board, espn, min_points=0)
        deltas = out["delta_ppg"].to_list()
        assert deltas == sorted(deltas, reverse=True)

    def test_agreement_stats_are_sane(self, board, espn):
        stats = agreement_stats(season_comparison(board, espn, min_points=0))
        assert stats["n"] == 3
        assert 0 <= stats["gridiron_higher_pct"] <= 1
        assert stats["largest_gap_ppg"] >= stats["median_abs_diff_ppg"]

    def test_disagreements_split_both_directions(self, board, espn):
        d = disagreements(season_comparison(board, espn, min_points=0), n=5, min_ppg=0)
        assert d["gridiron_higher"][0]["merge_name"] == "jamarrchase"
        assert d["espn_higher"][0]["merge_name"] == "bijanrobinson"

    def test_empty_inputs_do_not_raise(self):
        assert season_comparison(pl.DataFrame(), pl.DataFrame()).is_empty()
        assert disagreements(pl.DataFrame())["gridiron_higher"] == []
        assert agreement_stats(pl.DataFrame()) == {}

    def test_roster_report_scopes_to_players_you_own(self, board, espn):
        comp = season_comparison(board, espn, min_points=0)
        mine = pl.DataFrame({"merge_name": ["jamarrchase", "traviskelce"]})
        rep = roster_report(mine, comp)
        assert rep["summary"]["matched"] == 2
        assert rep["summary"]["gridiron_total_ppg"] == pytest.approx(19.0 + 11.0)
        assert rep["summary"]["espn_total_ppg"] == pytest.approx(16.5 + 11.9)


class TestWeeklyAccuracy:
    def _frames(self, n=120, seed=0):
        """Build a week where Gridiron is deliberately the better forecast."""
        rng = np.random.default_rng(seed)
        names = [f"player{i}" for i in range(n)]
        truth = rng.gamma(2.0, 5.0, n)
        gridiron = truth + rng.normal(0, 3.0, n)      # tighter noise
        espn = truth + rng.normal(0, 6.0, n)          # looser noise
        board = pl.DataFrame({
            "player_name": names, "merge_name": names,
            "position": ["WR"] * n, "team": ["CIN"] * n,
            "proj_points": gridiron, "floor": gridiron - 4, "ceiling": gridiron + 6,
        })
        espn_week = pl.DataFrame({
            "merge_name": names, "espn_week_proj": espn, "espn_week_actual": truth,
            "slot": ["WR"] * n, "started": [True] * n, "fantasy_team": ["A"] * n,
        })
        return board, espn_week

    def test_weekly_join_produces_both_projections(self):
        board, espn_week = self._frames()
        out = weekly_comparison(board, espn_week, week=3)
        assert out.height == 120
        assert {"gridiron_proj", "espn_week_proj", "espn_week_actual", "delta"} <= set(out.columns)
        assert out["week"].unique().to_list() == [3]

    def test_closer_column_marks_the_better_forecast(self):
        board, espn_week = self._frames()
        out = weekly_comparison(board, espn_week, week=3)
        assert "closer" in out.columns
        assert set(out["closer"].to_list()) <= {"gridiron", "espn", "tie"}

    def test_scorecard_detects_the_more_accurate_model(self):
        board, espn_week = self._frames()
        card = accuracy_scorecard(weekly_comparison(board, espn_week, week=3))
        assert card["scored"] == 120
        # Gridiron was constructed with half the noise, so it must win.
        assert card["gridiron"]["mae"] < card["espn"]["mae"]
        assert card["gridiron_win_rate"] > 0.5
        assert "Gridiron is more accurate" in card["verdict"]

    def test_scorecard_is_symmetric(self):
        """Swap the two forecasts and ESPN must win by the same logic."""
        board, espn_week = self._frames()
        swapped_board = board.with_columns(pl.Series("proj_points", espn_week["espn_week_proj"]))
        swapped_espn = espn_week.with_columns(pl.Series("espn_week_proj", board["proj_points"]))
        card = accuracy_scorecard(weekly_comparison(swapped_board, swapped_espn, week=3))
        assert card["espn"]["mae"] < card["gridiron"]["mae"]
        assert "ESPN is more accurate" in card["verdict"]

    def test_unplayed_games_are_not_scored(self):
        board, espn_week = self._frames(n=40)
        unplayed = espn_week.with_columns(pl.lit(0.0).alias("espn_week_actual"))
        card = accuracy_scorecard(weekly_comparison(board, unplayed, week=1))
        assert card["scored"] == 0
        assert "no completed games" in card["note"]

    def test_small_samples_refuse_to_declare_a_winner(self):
        board, espn_week = self._frames(n=20)
        card = accuracy_scorecard(weekly_comparison(board, espn_week, week=1))
        assert "Too early to call" in card["verdict"]

    def test_ties_are_reported(self):
        board, espn_week = self._frames(n=80)
        card = accuracy_scorecard(weekly_comparison(board, espn_week, week=1))
        total = card["gridiron_win_rate"] + card["espn_win_rate"] + card["ties"]
        assert total == pytest.approx(1.0, abs=1e-9)


def test_library_is_installed():
    assert espn_available(), "pip install espn_api"
