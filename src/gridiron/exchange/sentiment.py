"""Public market sentiment: what the crowd currently thinks, with no account.

Both venues serve prices, volume and books to anyone who asks — no key, no
signing, no login. This module reads only, and is deliberately separate from
the trading path: nothing here can place an order, and none of it needs
credentials, so the Markets page works on a fresh install.

The distinction from ``router.scan`` matters. That prices contracts *against
this model* and only surfaces disagreements it can act on. This answers a
plainer question: what is the market saying, how confident is it, how much
money stands behind it, and do the two venues agree with each other.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

from ..quant.odds import devig
from .base import Market
from .matching import classify_market
from .router import extract_teams

log = logging.getLogger(__name__)

# Below this, a "price" is one stale resting order rather than an opinion.
MIN_LIQUID_VOLUME = 25.0


@dataclass
class Quote:
    """One venue's public view of one contract."""

    venue: str
    market_id: str
    ticker: str
    title: str
    kind: str                       # moneyline | spread | total | team_total | other
    line: float | None
    teams: list[str]
    probability: float | None       # mid, in probability units
    bid: float | None
    ask: float | None
    spread: float | None
    volume: float
    open_interest: float
    close_time: str = ""

    @property
    def liquid(self) -> bool:
        return self.volume >= MIN_LIQUID_VOLUME

    @property
    def confidence(self) -> float:
        """How much weight this quote deserves in a consensus.

        Two things make a price informative: money behind it and a tight
        spread. A wide market on no volume is a guess with a ticker.
        """
        if self.probability is None:
            return 0.0
        money = math.log1p(max(self.volume, 0.0))
        tightness = 1.0 / (1.0 + 20.0 * (self.spread if self.spread is not None else 0.10))
        return money * tightness

    def as_dict(self) -> dict:
        return {
            "venue": self.venue, "market_id": self.market_id, "ticker": self.ticker,
            "title": self.title, "kind": self.kind, "line": self.line, "teams": self.teams,
            "probability": self.probability, "bid": self.bid, "ask": self.ask,
            "spread": self.spread, "volume": self.volume,
            "open_interest": self.open_interest, "close_time": self.close_time,
            "liquid": self.liquid, "confidence": round(self.confidence, 3),
        }


@dataclass
class Consensus:
    """What both venues together say about one question."""

    key: str
    kind: str
    line: float | None
    teams: list[str]
    label: str
    probability: float | None
    venues: list[str] = field(default_factory=list)
    disagreement: float | None = None     # spread between venue probabilities
    volume: float = 0.0
    open_interest: float = 0.0
    quotes: list[Quote] = field(default_factory=list)
    overround: float | None = None        # set when both sides were quoted

    def as_dict(self) -> dict:
        return {
            "key": self.key, "kind": self.kind, "line": self.line, "teams": self.teams,
            "label": self.label, "probability": self.probability, "venues": self.venues,
            "disagreement": self.disagreement, "volume": self.volume,
            "open_interest": self.open_interest, "overround": self.overround,
            "quotes": [q.as_dict() for q in self.quotes],
        }


def to_quote(market: Market) -> Quote:
    kind, line = classify_market(market.title, market.ticker)
    return Quote(
        venue=market.venue, market_id=market.market_id, ticker=market.ticker,
        title=market.title, kind=kind, line=line,
        teams=extract_teams(f"{market.title} {market.ticker}"),
        probability=market.mid, bid=market.yes_bid, ask=market.yes_ask,
        spread=market.spread, volume=float(market.volume or 0.0),
        open_interest=float(market.open_interest or 0.0), close_time=market.close_time,
    )


def game_key(teams: list[str]) -> str:
    """The fixture, regardless of which side a contract is written from."""
    return "-".join(sorted(teams[:2])) or "unknown"


def _key(q: Quote) -> str:
    """Same question, whichever venue is asking it.

    The game is the sorted pair, so a Kalshi contract naming the away side and
    a Polymarket one naming the home side land in the same fixture. But the
    subject — the team the YES side is *about* — stays in the key, because
    "Chiefs win" and "Eagles win" are opposite bets on one game and averaging
    their prices produces a number that means nothing.

    Lines round to a half point so 3.5 and 3.5000001 are one question.
    """
    line = "" if q.line is None else f"@{round(q.line * 2) / 2:g}"
    subject = q.teams[0] if q.teams else ""
    return f"{game_key(q.teams)}:{q.kind}{line}:{subject}"


def _label(q: Quote) -> str:
    if q.kind == "moneyline" and q.teams:
        return f"{q.teams[0]} to win"
    if q.kind == "spread" and q.teams:
        return f"{q.teams[0]} {q.line:+g}" if q.line is not None else f"{q.teams[0]} spread"
    if q.kind in ("total", "team_total") and q.line is not None:
        return f"{'team ' if q.kind == 'team_total' else ''}total {q.line:g}"
    return q.title[:60]


def build_consensus(quotes: list[Quote]) -> list[Consensus]:
    """Fold per-venue quotes into one view per question.

    Where both venues quote the same thing, the consensus is confidence
    weighted rather than a plain average — a $2m Polymarket book and a $300
    Kalshi market are not equally informative, and treating them as such is how
    you end up chasing an illiquid outlier.
    """
    groups: dict[str, list[Quote]] = {}
    for q in quotes:
        if q.probability is None or not (0.0 < q.probability < 1.0):
            continue
        groups.setdefault(_key(q), []).append(q)

    out: list[Consensus] = []
    for key, qs in groups.items():
        weights = [q.confidence for q in qs]
        total_w = sum(weights)
        if total_w > 0:
            prob = sum(q.probability * w for q, w in zip(qs, weights)) / total_w
        else:
            prob = sum(q.probability for q in qs) / len(qs)

        by_venue = {}
        for q in qs:
            by_venue.setdefault(q.venue, []).append(q.probability)
        venue_means = [sum(v) / len(v) for v in by_venue.values()]
        disagreement = (max(venue_means) - min(venue_means)) if len(venue_means) > 1 else None

        head = qs[0]
        out.append(Consensus(
            key=key, kind=head.kind, line=head.line, teams=head.teams, label=_label(head),
            probability=prob, venues=sorted(by_venue),
            disagreement=disagreement,
            volume=sum(q.volume for q in qs),
            open_interest=sum(q.open_interest for q in qs),
            quotes=sorted(qs, key=lambda q: -q.confidence),
        ))
    _pair_complements(out)
    out.sort(key=lambda c: -c.volume)
    return out


def _pair_complements(entries: list[Consensus]) -> None:
    """Devig two sides of the same game against each other, where both exist.

    Two complementary contracts price to more than 100%; the excess is the
    venue's margin. When both sides are quoted we can remove it exactly, which
    is strictly better than assuming a house edge.
    """
    by_game: dict[tuple, list[Consensus]] = {}
    for c in entries:
        if c.kind not in ("moneyline", "spread") or c.probability is None:
            continue
        by_game.setdefault((game_key(c.teams), c.kind, c.line), []).append(c)

    for group in by_game.values():
        if len(group) != 2:
            continue
        a, b = group
        if a.teams[:1] == b.teams[:1]:
            continue                      # same side twice, not a complement
        total = a.probability + b.probability
        if not 0.85 < total < 1.25:
            continue                      # not actually complementary; leave alone
        fair_a, fair_b = devig_pair(a.probability, b.probability)
        a.probability, b.probability = fair_a, fair_b
        a.overround = b.overround = round(total - 1.0, 4)


def devig_pair(a: float, b: float, method: str = "power") -> tuple[float, float]:
    """Strip the house edge from two sides that should sum to one.

    Two complementary contracts price to more than 100% — the overround is the
    venue's margin. Removing it is what turns a price into a probability.
    """
    fair = devig([a, b], method=method)
    return float(fair[0]), float(fair[1])


def snapshot(router, nfl_only: bool = True, limit: int = 500) -> dict:
    """Everything the Markets page needs, without an account.

    Venue failures are reported rather than raised: one venue being unreachable
    should degrade the page to the other, not empty it.
    """
    started = time.perf_counter()
    quotes: list[Quote] = []
    venues: list[dict] = []

    for name, client in router.venues.items():
        row = {"venue": name, "reachable": False, "markets": 0, "detail": "",
               "authenticated": bool(getattr(client, "authenticated", False))}
        try:
            fn = getattr(client, "list_nfl_markets", None) if nfl_only else None
            found = fn(limit) if fn else client.list_markets(limit=limit)
            for m in found:
                quotes.append(to_quote(m))
            row.update(reachable=True, markets=len(found), detail="ok")
        except Exception as exc:  # noqa: BLE001
            row["detail"] = str(exc)[:200]
            log.info("%s public market read failed: %s", name, exc)
        venues.append(row)

    consensus = build_consensus(quotes)
    priced = [c for c in consensus if c.probability is not None]
    return {
        "venues": venues,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seconds": round(time.perf_counter() - started, 2),
        "quote_count": len(quotes),
        "question_count": len(consensus),
        "total_volume": round(sum(c.volume for c in consensus), 2),
        "both_venues": sum(1 for c in consensus if len(c.venues) > 1),
        "widest_disagreement": max(
            (c.as_dict() for c in consensus if c.disagreement is not None),
            key=lambda c: c["disagreement"], default=None),
        "consensus": [c.as_dict() for c in priced],
    }
