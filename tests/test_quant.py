import numpy as np
import pytest

from gridiron.quant import (american_to_decimal, american_to_prob, brier_score,
                            closing_line_value, compute_edge, correlated_kelly,
                            cover_probability, devig, fractional_kelly, kelly_fraction,
                            log_loss, overround, price_game, prob_to_american,
                            reliability_curve, win_probability)
from gridiron.quant.calibration import IsotonicCalibrator
from gridiron.quant.ensemble import blend_with_market, log_odds_pool
from gridiron.quant.game_model import GameLine, implied_volatility, live_win_probability
from gridiron.quant.microstructure import BookLevel, OrderBook, order_flow_imbalance


class TestOdds:
    def test_american_decimal_roundtrip(self):
        for odds in (-250, -110, 100, 145, 900):
            assert prob_to_american(american_to_prob(odds)) == pytest.approx(odds, rel=1e-6)

    def test_favourite_has_decimal_below_two(self):
        assert american_to_decimal(-150) < 2.0 < american_to_decimal(+150)

    @pytest.mark.parametrize("method", ["multiplicative", "additive", "power", "shin", "logarithmic"])
    def test_every_devig_method_sums_to_one(self, method):
        raw = np.array([american_to_prob(-160), american_to_prob(+135)])
        assert devig(raw, method).sum() == pytest.approx(1.0, abs=1e-9)

    def test_devig_reduces_both_probabilities(self):
        raw = np.array([0.62, 0.45])
        fair = devig(raw, "power")
        assert (fair < raw).all()

    def test_shin_shades_the_longshot_harder_than_multiplicative(self):
        # The favourite-longshot bias means the raw price of a longshot
        # overstates its true chance, so a bias-correcting method should take
        # more away from the underdog than a proportional one does.
        raw = np.array([0.80, 0.28])
        assert devig(raw, "shin")[1] < devig(raw, "multiplicative")[1]
        assert devig(raw, "shin")[0] > devig(raw, "multiplicative")[0]

    def test_overround_is_the_excess(self):
        assert overround([0.55, 0.5]) == pytest.approx(0.05)

    def test_edge_is_zero_at_a_fair_price(self):
        e = compute_edge(0.55, 0.55)
        assert e.edge == pytest.approx(0.0)
        assert e.ev_per_unit == pytest.approx(0.0, abs=1e-9)

    def test_fee_reduces_expected_value(self):
        assert compute_edge(0.6, 0.5, fee=0.05).ev_per_unit < compute_edge(0.6, 0.5).ev_per_unit


class TestGameModel:
    def test_pick_em_is_a_coin_flip(self):
        assert win_probability(0.0) == pytest.approx(0.5, abs=0.01)

    def test_favourite_wins_more_often(self):
        assert win_probability(7.0) > win_probability(3.0) > win_probability(0.0)

    def test_cover_probability_is_half_at_the_line(self):
        assert cover_probability(3.5, 3.5) == pytest.approx(0.5, abs=1e-6)

    def test_implied_points_split_the_total(self):
        line = GameLine("KC", "BUF", spread=3.0, total=47.0)
        assert line.home_implied + line.away_implied == pytest.approx(47.0)
        assert line.home_implied - line.away_implied == pytest.approx(3.0)

    def test_live_win_probability_converges_to_the_result(self):
        assert live_win_probability(10, 1, 0.0) > 0.99
        assert live_win_probability(-10, 1, 0.0) < 0.01

    def test_a_lead_is_worth_more_later(self):
        early = live_win_probability(7, 2700)
        late = live_win_probability(7, 300)
        assert late > early

    def test_implied_volatility_inverts_the_spread(self):
        sd = 13.0
        p = win_probability(6.0, margin_sd=sd)
        assert implied_volatility(6.0, p) == pytest.approx(sd, rel=0.02)

    def test_price_sheet_has_both_sides(self):
        sheet = price_game(GameLine("KC", "BUF", 2.5, 48.5))
        assert sheet["home_win_prob"] + sheet["away_win_prob"] == pytest.approx(1.0)


class TestKelly:
    def test_no_edge_means_no_bet(self):
        assert kelly_fraction(0.5, 0.5) == 0.0
        assert kelly_fraction(0.4, 0.5) == 0.0

    def test_bigger_edge_means_bigger_bet(self):
        assert kelly_fraction(0.7, 0.5) > kelly_fraction(0.6, 0.5) > 0

    def test_fractional_kelly_is_capped(self):
        assert fractional_kelly(0.99, 0.10, fraction=1.0, cap=0.05) == pytest.approx(0.05)

    def test_uncertainty_shrinks_the_bet(self):
        from gridiron.quant.kelly import kelly_with_uncertainty
        confident = kelly_with_uncertainty(0.60, 0.50, prob_sd=0.01)
        unsure = kelly_with_uncertainty(0.60, 0.50, prob_sd=0.20)
        assert confident > unsure

    def test_correlation_reduces_total_exposure(self):
        probs = np.array([0.60, 0.60])
        prices = np.array([0.50, 0.50])
        independent = correlated_kelly(probs, prices, np.eye(2))
        correlated = correlated_kelly(probs, prices, np.array([[1.0, 0.9], [0.9, 1.0]]))
        assert correlated.gross_exposure < independent.gross_exposure


class TestCalibration:
    def test_perfect_forecast_scores_zero(self):
        y = np.array([1.0, 0.0, 1.0, 0.0])
        assert brier_score(y, y) == pytest.approx(0.0)

    def test_log_loss_punishes_confident_errors(self):
        y = np.array([1.0, 1.0])
        assert log_loss(np.array([0.99, 0.99]), y) < log_loss(np.array([0.01, 0.01]), y)

    def test_reliability_curve_covers_every_prediction(self):
        rng = np.random.default_rng(1)
        p = rng.random(500)
        y = (rng.random(500) < p).astype(float)
        curve = reliability_curve(p, y)
        assert sum(b["count"] for b in curve["bins"]) == 500
        assert curve["expected_calibration_error"] < 0.15

    def test_isotonic_calibrator_is_monotone(self):
        rng = np.random.default_rng(2)
        p = rng.random(800)
        y = (rng.random(800) < p ** 2).astype(float)   # over-confident forecaster
        cal = IsotonicCalibrator.fit(p, y)
        out = cal(np.linspace(0.05, 0.95, 20))
        assert np.all(np.diff(out) >= -1e-9)

    def test_closing_line_value_signs_correctly(self):
        clv = closing_line_value(np.array([0.50]), np.array([0.55]), np.array([1.0]))
        assert clv["mean_clv"] > 0
        clv_short = closing_line_value(np.array([0.50]), np.array([0.55]), np.array([-1.0]))
        assert clv_short["mean_clv"] < 0


class TestEnsemble:
    def test_pooling_two_identical_forecasts_is_a_no_op(self):
        assert log_odds_pool(np.array([[0.7], [0.7]]))[0] == pytest.approx(0.7)

    def test_blend_sits_between_model_and_market(self):
        out = blend_with_market(0.70, 0.50, 0.35)
        assert 0.50 < out < 0.70

    def test_blend_cap_bounds_the_move(self):
        out = float(blend_with_market(0.999, 0.30, 1.0, max_shift=0.5))
        assert out < 0.45


class TestMicrostructure:
    def test_micro_price_leans_toward_the_heavier_side(self):
        book = OrderBook(bids=[BookLevel(0.54, 900)], asks=[BookLevel(0.56, 100)])
        assert book.micro_price() > book.mid

    def test_imbalance_sign_follows_depth(self):
        heavy_bid = OrderBook(bids=[BookLevel(0.54, 900)], asks=[BookLevel(0.56, 100)])
        heavy_ask = OrderBook(bids=[BookLevel(0.54, 100)], asks=[BookLevel(0.56, 900)])
        assert heavy_bid.imbalance() > 0 > heavy_ask.imbalance()

    def test_sweep_cost_is_worse_than_the_touch(self):
        book = OrderBook(bids=[], asks=[BookLevel(0.56, 100), BookLevel(0.58, 400)])
        assert book.sweep_cost(300, "buy") > 0.56

    def test_order_flow_imbalance_is_positive_when_bids_build(self):
        prev = OrderBook(bids=[BookLevel(0.54, 100)], asks=[BookLevel(0.56, 100)])
        curr = OrderBook(bids=[BookLevel(0.54, 400)], asks=[BookLevel(0.56, 100)])
        assert order_flow_imbalance(prev, curr) > 0
