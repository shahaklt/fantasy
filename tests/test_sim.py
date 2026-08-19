import numpy as np
import pytest

from gridiron.models.availability import availability_params, simulate_availability
from gridiron.scoring import LeagueSettings
from gridiron.sim.backend import make_backend
from gridiron.sim.constants import CALIBRATION
from gridiron.sim.league import optimal_lineups, round_robin


class TestBackend:
    def test_gamma_at_zero_shape_is_zero(self):
        xp = make_backend(seed=1)
        out = xp.to_numpy(xp.gamma(np.array([0.0, 2.0]), np.array([1.0, 1.0])))
        assert out[0] == 0.0
        assert out[1] > 0.0

    def test_binomial_respects_its_bounds(self):
        xp = make_backend(seed=1)
        out = xp.to_numpy(xp.binomial(np.full(500, 10.0), np.full(500, 0.5)))
        assert out.min() >= 0 and out.max() <= 10

    def test_draws_are_reproducible_for_a_seed(self):
        a = make_backend(seed=42).to_numpy(make_backend(seed=42).normal((5,), 0.0, 1.0))
        b = make_backend(seed=42).to_numpy(make_backend(seed=42).normal((5,), 0.0, 1.0))
        assert np.allclose(a, b)

    def test_shifted_gamma_reproduces_per_carry_yards(self):
        """The rushing distribution should recover ~4.3 yards a carry."""
        xp = make_backend(seed=3)
        k, shift = CALIBRATION.rush_yards_shape, CALIBRATION.rush_yards_shift
        ypc = 4.33
        scale = (ypc + shift) / k
        draws = xp.to_numpy(xp.gamma(np.full(200_000, k), scale)) - shift
        assert draws.mean() == pytest.approx(ypc, abs=0.05)
        assert 5.0 < draws.std() < 7.5      # real per-carry sd is ~6.2


class TestAvailability:
    def test_injuries_persist_across_weeks(self):
        """A two-state chain should produce multi-week absences, not coin flips."""
        model = availability_params(np.array(["RB"] * 3))
        rng = np.random.default_rng(0)
        active = simulate_availability(model, 4000, 17, rng)
        games = active.sum(axis=1)
        assert 14.0 < games.mean() < 16.5
        # An i.i.d. model with p=0.9 essentially never yields a 10-game season.
        assert (games <= 10).mean() > 0.02

    def test_older_backs_are_less_durable(self):
        young = availability_params(np.array(["RB"]), ages=np.array([23.0]))
        old = availability_params(np.array(["RB"]), ages=np.array([31.0]))
        assert old.healthy_rate[0] < young.healthy_rate[0]

    def test_observed_durability_moves_the_estimate(self):
        fragile = availability_params(np.array(["WR"]), games_missed_rate=np.array([0.5]))
        iron = availability_params(np.array(["WR"]), games_missed_rate=np.array([0.0]))
        assert fragile.healthy_rate[0] < iron.healthy_rate[0]


class TestLineups:
    def test_flex_takes_the_best_leftover(self):
        pts = np.array([[20.0, 18.0, 15.0, 14.0, 9.0]])
        pos = np.array(["RB", "RB", "RB", "WR", "WR"])
        total, used = optimal_lineups(pts, pos, {"RB": 2, "WR": 1, "FLEX": 1})
        # RB 20 + RB 18 + WR 14 + flex takes RB 15 over WR 9
        assert total[0] == pytest.approx(20 + 18 + 14 + 15)

    def test_a_slot_with_nobody_eligible_is_skipped(self):
        pts = np.array([[12.0]])
        pos = np.array(["WR"])
        total, _ = optimal_lineups(pts, pos, {"QB": 1, "WR": 1})
        assert total[0] == pytest.approx(12.0)

    def test_no_player_fills_two_slots(self):
        pts = np.array([[30.0, 1.0, 1.0]])
        pos = np.array(["RB", "RB", "WR"])
        _, used = optimal_lineups(pts, pos, {"RB": 1, "FLEX": 1})
        assert used[0].sum() == 2

    def test_round_robin_pairs_everyone_once_a_week(self):
        schedule = round_robin(12, 14)
        assert len(schedule) == 14
        for week in schedule:
            teams = [t for pair in week for t in pair]
            assert len(teams) == len(set(teams)) == 12
