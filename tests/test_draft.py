import numpy as np
import polars as pl
import pytest

from gridiron.draft.availability import snake_picks, survival_probability
from gridiron.draft.simulator import DraftSimulator, DraftState, optimal_lineup_points
from gridiron.draft.vbd import add_value_columns, assign_tiers, positional_scarcity, replacement_levels
from gridiron.scoring import LeagueSettings


class TestPickOrder:
    def test_snake_order_reverses_each_round(self):
        picks = snake_picks(draft_slot=1, teams=12, rounds=4)
        assert picks == [1, 24, 25, 48]

    def test_last_slot_gets_the_turn(self):
        picks = snake_picks(draft_slot=12, teams=12, rounds=3)
        assert picks == [12, 13, 36]

    def test_pick_count_matches_rounds(self):
        assert len(snake_picks(6, 10, 16)) == 16


class TestAvailabilityCurve:
    def test_a_player_at_his_adp_is_a_coin_flip(self):
        p = survival_probability(np.array([20.0]), np.array([8.0]), pick=20)
        assert p[0] == pytest.approx(0.5, abs=0.02)

    def test_availability_falls_as_the_draft_moves_on(self):
        adp, sd = np.array([20.0]), np.array([8.0])
        assert (survival_probability(adp, sd, 5)[0]
                > survival_probability(adp, sd, 20)[0]
                > survival_probability(adp, sd, 40)[0])

    def test_a_more_certain_player_falls_off_faster(self):
        certain = survival_probability(np.array([20.0]), np.array([2.0]), 30)[0]
        uncertain = survival_probability(np.array([20.0]), np.array([25.0]), 30)[0]
        assert uncertain > certain


class TestValueBasedDrafting:
    def test_replacement_level_is_deeper_for_wide_receivers(self, fake_board, league):
        levels = replacement_levels(fake_board, league)
        # More WRs start than TEs, so the WR baseline sits further down the board.
        assert levels.counts["WR"] > levels.counts["TE"]

    def test_vorp_ranks_scarcity_not_raw_points(self, fake_board, league):
        board = add_value_columns(fake_board, league)
        top_qb = board.filter(pl.col("position") == "QB").sort("proj_points", descending=True)
        top_rb = board.filter(pl.col("position") == "RB").sort("proj_points", descending=True)
        # QB1 outscores RB1 here, but only one QB starts per team.
        assert top_qb["proj_points"][0] > top_rb["proj_points"][0]
        assert top_qb["vorp"][0] < top_rb["vorp"][0]

    def test_auction_dollars_respect_the_budget(self, fake_board, league):
        board = add_value_columns(fake_board, league)
        spend = board["auction_value"].sum()
        budget = league.teams * league.auction_budget
        assert spend == pytest.approx(budget, rel=0.02)

    def test_every_drafted_player_costs_at_least_a_dollar(self, fake_board, league):
        board = add_value_columns(fake_board, league)
        drafted = board.head(league.total_picks)
        assert drafted["auction_value"].min() >= 1.0

    def test_tiers_are_ordered_within_a_position(self, fake_board, league):
        board = assign_tiers(add_value_columns(fake_board, league))
        rbs = board.filter(pl.col("position") == "RB").sort("vorp", descending=True)
        assert rbs["tier"].to_list() == sorted(rbs["tier"].to_list())

    def test_scarcity_reflects_what_is_gone(self, fake_board, league):
        board = assign_tiers(add_value_columns(fake_board, league))
        before = positional_scarcity(board, league)
        gone = set(board.filter(pl.col("position") == "RB")
                   .sort("proj_points", descending=True).head(10)["player_id"].to_list())
        after = positional_scarcity(board, league, drafted_ids=gone)
        rb_before = before.filter(pl.col("position") == "RB")["best_available"][0]
        rb_after = after.filter(pl.col("position") == "RB")["best_available"][0]
        assert rb_after < rb_before


class TestDraftSimulation:
    def test_state_tracks_the_snake_order(self, league):
        state = DraftState(league=league, my_slot=5)
        assert state.team_on_clock(1) == 1
        assert state.team_on_clock(12) == 12
        assert state.team_on_clock(13) == 12      # snake turns
        assert state.team_on_clock(24) == 1

    def test_recording_a_pick_advances_the_draft(self, league):
        state = DraftState(league=league, my_slot=1)
        state.record("p0001")
        assert state.pick_number == 2
        assert state.roster(1) == ["p0001"]

    def test_undo_restores_the_previous_state(self, league):
        state = DraftState(league=league, my_slot=1)
        state.record("p0001")
        state.undo()
        assert state.pick_number == 1
        assert state.roster(1) == []

    def test_simulation_fills_a_legal_roster(self, fake_board, league):
        sim = DraftSimulator(fake_board, league, seed=1)
        state = DraftState(league=league, my_slot=3)
        roster = sim.simulate_remaining(np.zeros(len(sim.ids), dtype=bool), state)
        assert len(roster) == league.roster_size
        assert len(set(roster)) == len(roster)          # nobody drafted twice

    def test_opponents_do_not_draft_the_same_player_twice(self, fake_board, league):
        sim = DraftSimulator(fake_board, league, seed=2)
        state = DraftState(league=league, my_slot=1)
        taken = np.zeros(len(sim.ids), dtype=bool)
        roster = sim.simulate_remaining(taken, state)
        assert taken.sum() == 0                          # the caller's mask is untouched
        assert len(set(roster)) == len(roster)

    def test_recommendation_prefers_the_stronger_roster(self, fake_board, league):
        sim = DraftSimulator(fake_board, league, seed=3)
        state = DraftState(league=league, my_slot=1)
        rec = sim.evaluate_candidates(state, n_sims=12, top_k=5)
        assert rec.height > 0
        assert rec["roster_value"][0] >= rec["roster_value"][-1]
        assert rec["value_vs_best"][0] == pytest.approx(0.0)

    def test_lineup_points_never_exceed_the_whole_roster(self):
        pts = np.array([25.0, 20.0, 15.0, 10.0, 5.0])
        pos = np.array(["QB", "RB", "RB", "WR", "WR"])
        total = optimal_lineup_points(pts, pos, {"QB": 1, "RB": 2, "WR": 1})
        assert total <= pts.sum()
        assert total == pytest.approx(25 + 20 + 15 + 10)
