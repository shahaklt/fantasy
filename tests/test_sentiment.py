"""Public sentiment: read-only, credential-free, and honest about liquidity.

The live venues are unreachable from CI, so these drive the aggregation with
synthetic quotes. That is the half worth testing anyway — the HTTP call is one
requests.get, while the folding of two venues into one view is where the
judgement lives.
"""
from __future__ import annotations

import pytest

from gridiron.exchange.base import Market
from gridiron.exchange.sentiment import (Quote, build_consensus, devig_pair, snapshot,
                                         to_quote)


def market(venue, ticker, title, bid, ask, volume=1000.0, oi=0.0):
    return Market(venue=venue, market_id=f"{venue}-{ticker}", ticker=ticker, title=title,
                  yes_bid=bid, yes_ask=ask, volume=volume, open_interest=oi)


class FakeVenue:
    """Answers public reads; has no credentials and cannot trade."""

    authenticated = False

    def __init__(self, markets, fail=False):
        self._markets, self._fail = markets, fail

    def list_nfl_markets(self, limit=500):
        if self._fail:
            raise RuntimeError("connection refused")
        return self._markets[:limit]

    list_markets = lambda self, limit=500, **kw: self.list_nfl_markets(limit)  # noqa: E731


class FakeRouter:
    def __init__(self, venues):
        self.venues = venues


# --------------------------------------------------------------------- quotes
def test_a_quote_carries_the_kind_and_the_teams():
    q = to_quote(market("kalshi", "KXNFLGAME-CHIEFS", "Chiefs to beat the Eagles", 0.60, 0.62))
    assert q.kind == "moneyline"
    assert q.teams[0] == "KC", "the team named first is the subject of the bet"
    assert q.probability == pytest.approx(0.61)
    assert q.spread == pytest.approx(0.02)


def test_confidence_rewards_money_and_punishes_a_wide_spread():
    tight = Quote("kalshi", "1", "t", "t", "moneyline", None, [], 0.5, 0.49, 0.51, 0.02, 5000, 0)
    wide = Quote("kalshi", "2", "t", "t", "moneyline", None, [], 0.5, 0.40, 0.60, 0.20, 5000, 0)
    thin = Quote("kalshi", "3", "t", "t", "moneyline", None, [], 0.5, 0.49, 0.51, 0.02, 5, 0)
    assert tight.confidence > wide.confidence, "a wide market is a guess with a ticker"
    assert tight.confidence > thin.confidence, "volume is what makes a price informative"


def test_an_unpriced_market_is_dropped_not_guessed_at():
    q = to_quote(market("kalshi", "X", "Chiefs to win", None, None, volume=0))
    assert build_consensus([q]) == []


# ------------------------------------------------------------------ consensus
def test_both_venues_pricing_the_same_side_fold_together():
    quotes = [
        to_quote(market("kalshi", "A", "Chiefs beat Eagles", 0.60, 0.62)),
        to_quote(market("polymarket", "B", "Will the Chiefs defeat the Eagles?", 0.55, 0.57)),
    ]
    [c] = build_consensus(quotes)
    assert sorted(c.venues) == ["kalshi", "polymarket"]
    assert c.disagreement == pytest.approx(0.05, abs=1e-6)
    assert 0.56 < c.probability < 0.61


def test_opposite_sides_of_one_game_are_never_averaged():
    """"Chiefs win" and "Eagles win" are opposite bets; their mean means nothing."""
    quotes = [
        to_quote(market("kalshi", "A", "Chiefs beat Eagles", 0.60, 0.62)),
        to_quote(market("polymarket", "B", "Eagles beat Chiefs", 0.40, 0.42)),
    ]
    entries = build_consensus(quotes)
    assert len(entries) == 2, "one entry per side, not one blended nonsense number"
    assert {e.teams[0] for e in entries} == {"KC", "PHI"}


def test_both_sides_quoted_gets_devigged_against_itself():
    quotes = [
        to_quote(market("kalshi", "A", "Chiefs beat Eagles", 0.63, 0.65)),
        to_quote(market("kalshi", "B", "Eagles beat Chiefs", 0.39, 0.41)),
    ]
    entries = build_consensus(quotes)
    total = sum(e.probability for e in entries)
    assert total == pytest.approx(1.0, abs=1e-6), "the house edge should be removed exactly"
    assert entries[0].overround == pytest.approx(0.04, abs=1e-6)


def test_the_consensus_leans_on_the_venue_with_the_money():
    """A $2m book and a $300 market are not equally informative."""
    quotes = [
        to_quote(market("kalshi", "A", "Chiefs beat Eagles", 0.79, 0.81, volume=50)),
        to_quote(market("polymarket", "B", "Chiefs beat Eagles", 0.59, 0.61, volume=2_000_000)),
    ]
    [c] = build_consensus(quotes)
    assert c.probability < 0.65, "the thin market should not drag the consensus"


def test_different_lines_stay_different_questions():
    quotes = [
        to_quote(market("kalshi", "A", "Chiefs Eagles over 44.5 points", 0.50, 0.52)),
        to_quote(market("kalshi", "B", "Chiefs Eagles over 51.5 points", 0.30, 0.32)),
    ]
    assert len(build_consensus(quotes)) == 2, "a 44.5 total is not a 51.5 total"


def test_devig_removes_the_house_edge():
    a, b = devig_pair(0.55, 0.50)          # sums to 1.05
    assert a + b == pytest.approx(1.0, abs=1e-9)
    assert a > b


# ------------------------------------------------------------------- snapshot
def test_one_venue_failing_degrades_rather_than_empties():
    router = FakeRouter({
        "kalshi": FakeVenue([market("kalshi", "A", "Chiefs beat Eagles", 0.60, 0.62)]),
        "polymarket": FakeVenue([], fail=True),
    })
    body = snapshot(router)
    status = {v["venue"]: v for v in body["venues"]}
    assert status["kalshi"]["reachable"] and status["polymarket"]["reachable"] is False
    assert "connection refused" in status["polymarket"]["detail"]
    assert body["question_count"] == 1, "the reachable venue still reports"


def test_a_snapshot_needs_no_credentials():
    router = FakeRouter({"kalshi": FakeVenue([market("kalshi", "A", "Chiefs beat Eagles", 0.6, 0.62)])})
    body = snapshot(router)
    assert body["venues"][0]["authenticated"] is False
    assert body["consensus"], "public data must render on a fresh install"


def test_the_widest_disagreement_is_surfaced():
    router = FakeRouter({
        "kalshi": FakeVenue([market("kalshi", "A", "Chiefs beat Eagles", 0.70, 0.72),
                             market("kalshi", "B", "Bills beat Jets", 0.60, 0.62)]),
        "polymarket": FakeVenue([market("polymarket", "C", "Chiefs beat Eagles", 0.50, 0.52),
                                 market("polymarket", "D", "Bills beat Jets", 0.59, 0.61)]),
    })
    widest = snapshot(router)["widest_disagreement"]
    assert widest["teams"][0] in ("KC", "PHI")
    assert widest["disagreement"] > 0.15
