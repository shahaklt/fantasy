import numpy as np
import pytest

from gridiron.exchange.base import (Action, Market, Order, OrderType, PaperBroker,
                                    Position, Side, TradingMode)
from gridiron.exchange.demo import DemoVenue
from gridiron.exchange.kalshi import KalshiClient
from gridiron.exchange.matching import GameProbabilities, ProbabilityBook, classify_market
from gridiron.exchange.risk import RiskGuard, RiskLimits
from gridiron.exchange.router import MarketRouter, extract_teams
from gridiron.exchange.store import TickStore


class TestClassification:
    @pytest.mark.parametrize("title,kind", [
        ("Chiefs beat Bills", "moneyline"),
        ("Eagles to win the game", "moneyline"),
        ("Chiefs/Bills total points over 48.5", "total"),
        ("Will KC cover -3.5?", "spread"),
        ("Lions team total over 27.5", "team_total"),
        ("Something entirely unrelated", "unknown"),
    ])
    def test_market_kind(self, title, kind):
        assert classify_market(title)[0] == kind

    def test_cover_is_not_read_as_over(self):
        """'cover' contains 'over' -- substring matching would misclassify it."""
        assert classify_market("Will KC cover -3.5?")[0] == "spread"

    def test_line_keeps_its_sign(self):
        assert classify_market("KC cover -3.5")[1] == -3.5

    def test_teams_come_back_in_the_order_named(self):
        assert extract_teams("Eagles cover -3.5 vs Cowboys") == ["PHI", "DAL"]
        assert extract_teams("49ers beat Rams") == ["SF", "LA"]


class TestProbabilityBook:
    @pytest.fixture
    def book(self):
        rng = np.random.default_rng(0)
        home = rng.normal(27, 10, 40_000)
        away = rng.normal(20, 10, 40_000)
        b = ProbabilityBook()
        b.add(GameProbabilities("KC", "BUF", home, away, "g1"))
        return b

    def test_moneyline_favours_the_better_team(self, book):
        assert book.games[0].moneyline("KC") > 0.6
        assert book.games[0].moneyline("KC") + book.games[0].moneyline("BUF") == pytest.approx(1.0, abs=1e-6)

    def test_total_and_moneyline_get_different_answers(self, book):
        ml = book.price("Chiefs beat Bills", "", ["KC", "BUF"])
        total = book.price("Chiefs/Bills total points over 47.5", "", ["KC", "BUF"])
        assert ml is not None and total is not None
        assert ml[0] != total[0]
        assert "moneyline" in ml[1] and "total" in total[1]

    def test_subject_is_the_team_named_first(self, book):
        kc = book.price("Chiefs cover -3.5 vs Bills", "", ["KC", "BUF"])
        buf = book.price("Bills cover 3.5 vs Chiefs", "", ["BUF", "KC"])
        assert kc[1].startswith("KC") and buf[1].startswith("BUF")

    def test_unclassifiable_markets_are_skipped(self, book):
        assert book.price("Coin flip market", "", ["KC"]) is None

    def test_unknown_teams_are_skipped(self, book):
        assert book.price("Chiefs beat Bills", "", []) is None


class TestRiskGuard:
    def test_paper_is_the_default_mode(self, monkeypatch):
        monkeypatch.delenv("GRIDIRON_TRADING_MODE", raising=False)
        assert RiskGuard().mode == TradingMode.PAPER

    def test_live_needs_an_explicit_environment_opt_in(self, monkeypatch):
        monkeypatch.setenv("GRIDIRON_TRADING_MODE", "live")
        assert RiskGuard().mode == TradingMode.LIVE

    def test_live_order_is_refused_without_confirmation(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GRIDIRON_TRADING_MODE", "live")
        guard = RiskGuard(limits=RiskLimits(), bankroll=1000)
        order = Order(venue="kalshi", market_id="X", side=Side.YES, action=Action.BUY,
                      quantity=10, price=0.5)
        assert guard.check(order, confirmed=False).allowed is False
        assert "confirmation" in guard.check(order, confirmed=False).reason

    def test_oversized_order_is_scaled_down_not_dropped(self):
        guard = RiskGuard(limits=RiskLimits(max_order_notional=10.0), bankroll=1000)
        order = Order(venue="k", market_id="X", side=Side.YES, action=Action.BUY,
                      quantity=1000, price=0.5)
        decision = guard.check(order)
        assert decision.allowed and decision.adjusted_quantity == 20

    def test_extreme_prices_are_refused(self):
        guard = RiskGuard(bankroll=1000)
        order = Order(venue="k", market_id="X", side=Side.YES, action=Action.BUY,
                      quantity=10, price=0.995)
        assert guard.check(order).allowed is False

    def test_thin_edges_are_refused(self):
        guard = RiskGuard(limits=RiskLimits(min_edge=0.05), bankroll=1000)
        order = Order(venue="k", market_id="X", side=Side.YES, action=Action.BUY,
                      quantity=5, price=0.5)
        assert guard.check(order, edge=0.01).allowed is False

    def test_market_exposure_is_capped(self):
        guard = RiskGuard(limits=RiskLimits(max_market_notional=20.0), bankroll=1000)
        held = [Position(venue="k", market_id="X", ticker="X", side=Side.YES,
                         quantity=30, avg_price=0.6)]
        order = Order(venue="k", market_id="X", side=Side.YES, action=Action.BUY,
                      quantity=10, price=0.5)
        assert guard.check(order, positions=held).allowed is False

    def test_kill_switch_stops_everything(self, monkeypatch, tmp_path):
        import gridiron.exchange.risk as risk_mod
        switch = tmp_path / "STOP_TRADING"
        switch.write_text("halt")
        monkeypatch.setattr(risk_mod, "KILL_SWITCH", switch)
        guard = risk_mod.RiskGuard(bankroll=1000)
        order = Order(venue="k", market_id="X", side=Side.YES, action=Action.BUY,
                      quantity=1, price=0.5)
        assert guard.check(order).allowed is False


class TestPaperBroker:
    def test_a_fill_moves_cash_into_a_position(self):
        broker = PaperBroker(starting_cash=100.0)
        order = Order(venue="paper", market_id="X", side=Side.YES, action=Action.BUY,
                      quantity=100, price=0.4)
        filled = broker.place_order(order)
        assert filled.status == "filled"
        assert broker.balance() == pytest.approx(60.0)
        assert broker.positions()[0].quantity == 100

    def test_orders_beyond_the_balance_are_rejected(self):
        broker = PaperBroker(starting_cash=10.0)
        order = Order(venue="paper", market_id="X", side=Side.YES, action=Action.BUY,
                      quantity=100, price=0.9)
        assert broker.place_order(order).status == "rejected"
        assert broker.balance() == pytest.approx(10.0)

    def test_equity_marks_the_position_to_market(self):
        broker = PaperBroker(starting_cash=100.0)
        broker.place_order(Order(venue="paper", market_id="X", side=Side.YES,
                                 action=Action.BUY, quantity=100, price=0.4))
        assert broker.equity({"X": 0.6}) == pytest.approx(120.0)


class TestDemoVenue:
    def test_it_quotes_a_two_sided_book(self):
        venue = DemoVenue(seed=1)
        market = venue.list_markets()[0]
        book = venue.get_orderbook(market.market_id)
        assert book.best_bid < book.best_ask
        assert book.spread > 0
        assert book.mid == pytest.approx((book.best_bid + book.best_ask) / 2)

    def test_prices_stay_inside_the_unit_interval(self):
        venue = DemoVenue(seed=2, volatility=0.05)
        for _ in range(50):
            venue._advance()
        assert all(0 < v < 1 for v in venue._fair.values())


class TestRouter:
    def test_evaluate_takes_the_side_the_model_prefers(self, tmp_path):
        router = MarketRouter(store=TickStore(tmp_path / "t.db"), bankroll=500)
        market = Market(venue="demo", market_id="m", ticker="m", title="t",
                        yes_bid=0.50, yes_ask=0.52)
        assert router.evaluate(market, 0.90).side == Side.YES
        assert router.evaluate(market, 0.10).side == Side.NO

    def test_no_edge_means_a_tiny_stake(self, tmp_path):
        router = MarketRouter(store=TickStore(tmp_path / "t.db"), bankroll=500)
        market = Market(venue="demo", market_id="m", ticker="m", title="t",
                        yes_bid=0.49, yes_ask=0.51)
        assert router.evaluate(market, 0.50).kelly_fraction == pytest.approx(0.0, abs=1e-6)

    def test_submit_is_blocked_without_a_broker(self, tmp_path):
        router = MarketRouter(store=TickStore(tmp_path / "t.db"), bankroll=500)
        market = Market(venue="demo", market_id="m", ticker="m", title="t",
                        yes_bid=0.40, yes_ask=0.42)
        signal = router.evaluate(market, 0.70)
        assert router.submit(signal, quantity=5, broker=None)["submitted"] is False

    def test_polling_writes_microstructure_features(self, tmp_path):
        venue = DemoVenue(seed=5)
        store = TickStore(tmp_path / "ticks.db")
        router = MarketRouter(venues={"demo": venue}, store=store, bankroll=500)
        markets = venue.list_markets()
        router.poll_once(markets)
        router.poll_once(markets)
        history = store.history(markets[0].market_id)
        assert len(history) == 2
        assert history[-1]["mid"] is not None
        assert history[-1]["micro_price"] is not None


class TestKalshiAuth:
    def test_read_only_client_needs_no_credentials(self):
        client = KalshiClient(credentials=None)
        assert client.authenticated is False

    def test_signing_without_credentials_is_a_clear_error(self):
        client = KalshiClient(credentials=None)
        with pytest.raises(RuntimeError, match="credentials"):
            client._sign("123", "GET", "/trade-api/v2/portfolio/balance")

    def test_subscribe_message_names_the_markets(self):
        import json
        msg = json.loads(KalshiClient().subscribe_message(["A", "B"]))
        assert msg["cmd"] == "subscribe"
        assert msg["params"]["market_tickers"] == ["A", "B"]

    def test_cents_convert_to_probabilities(self):
        market = KalshiClient._to_market({"ticker": "X", "title": "t",
                                          "yes_bid": 54, "yes_ask": 56})
        assert market.yes_bid == pytest.approx(0.54)
        assert market.mid == pytest.approx(0.55)


class TestKalshiSigning:
    """Verify the RSA-PSS signature is well formed.

    The live endpoint cannot be reached from a test, but the signature scheme
    can be checked end to end: sign the documented message with a generated key
    and verify it with the matching public key.
    """

    @pytest.fixture
    def signed(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        from gridiron.exchange.kalshi import KalshiCredentials

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()).decode()
        client = KalshiClient(credentials=KalshiCredentials("key-id-123", pem))
        return client, key.public_key()

    def test_signature_verifies_against_the_documented_message(self, signed):
        import base64

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        client, public_key = signed
        ts, method, path = "1700000000000", "GET", "/trade-api/v2/portfolio/balance"
        signature = client._sign(ts, method, path)
        public_key.verify(
            base64.b64decode(signature),
            f"{ts}{method}{path}".encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=hashes.SHA256().digest_size),
            hashes.SHA256())

    def test_headers_carry_the_key_id_and_a_timestamp(self, signed):
        client, _ = signed
        headers = client._headers("GET", "/trade-api/v2/portfolio/balance")
        assert headers["KALSHI-ACCESS-KEY"] == "key-id-123"
        assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()
        assert len(headers["KALSHI-ACCESS-SIGNATURE"]) > 100

    def test_the_signature_covers_the_method(self, signed):
        client, _ = signed
        get = client._sign("1700000000000", "GET", "/trade-api/v2/portfolio/orders")
        post = client._sign("1700000000000", "POST", "/trade-api/v2/portfolio/orders")
        assert get != post
