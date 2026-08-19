"""Ties the simulation's fair values to whatever the venues are quoting.

Responsibilities:
  * hold the configured venues and expose one merged market list;
  * match a quoted contract to the game/market the model has an opinion on;
  * turn (model probability, quoted price) into an edge, a blended forecast and
    a Kelly size;
  * poll books on a timer, writing microstructure features to the tick store so
    the sentiment panel has a history to draw.
"""
from __future__ import annotations

import logging
import re
import threading
import time

import numpy as np
from dataclasses import dataclass, field

from ..quant.ensemble import blend_with_market
from ..quant.kelly import kelly_with_uncertainty
from ..quant.microstructure import OrderBook, book_features
from ..quant.odds import compute_edge
from .base import Action, Market, Order, OrderType, Position, Side, TradingMode
from .risk import RiskGuard
from .store import TickStore

log = logging.getLogger(__name__)

#: Fee charged on winnings, by venue (Kalshi bills on profit; Polymarket is 0).
VENUE_FEES = {"kalshi": 0.02, "polymarket": 0.0, "paper": 0.0}

TEAM_ALIASES = {
    "arizona": "ARI", "cardinals": "ARI", "atlanta": "ATL", "falcons": "ATL",
    "baltimore": "BAL", "ravens": "BAL", "buffalo": "BUF", "bills": "BUF",
    "carolina": "CAR", "panthers": "CAR", "chicago": "CHI", "bears": "CHI",
    "cincinnati": "CIN", "bengals": "CIN", "cleveland": "CLE", "browns": "CLE",
    "dallas": "DAL", "cowboys": "DAL", "denver": "DEN", "broncos": "DEN",
    "detroit": "DET", "lions": "DET", "green bay": "GB", "packers": "GB",
    "houston": "HOU", "texans": "HOU", "indianapolis": "IND", "colts": "IND",
    "jacksonville": "JAX", "jaguars": "JAX", "kansas city": "KC", "chiefs": "KC",
    "las vegas": "LV", "raiders": "LV", "chargers": "LAC", "rams": "LA",
    "miami": "MIA", "dolphins": "MIA", "minnesota": "MIN", "vikings": "MIN",
    "new england": "NE", "patriots": "NE", "new orleans": "NO", "saints": "NO",
    "giants": "NYG", "jets": "NYJ", "philadelphia": "PHI", "eagles": "PHI",
    "pittsburgh": "PIT", "steelers": "PIT", "seattle": "SEA", "seahawks": "SEA",
    "san francisco": "SF", "49ers": "SF", "niners": "SF", "tampa": "TB",
    "buccaneers": "TB", "bucs": "TB", "tennessee": "TEN", "titans": "TEN",
    "washington": "WAS", "commanders": "WAS",
}


def extract_teams(text: str) -> list[str]:
    """NFL team codes found in a title, **ordered by where they appear**.

    Order carries meaning: "Eagles cover -3.5 vs Cowboys" is a bet on
    Philadelphia, and the only thing distinguishing the subject from the
    opponent is which is named first. Matching on the alias ("Eagles") rather
    than the code ("PHI") is what makes that position meaningful, since titles
    almost never contain the code.
    """
    if not text:
        return []
    lowered = text.lower()
    upper = text.upper()
    hits: dict[str, int] = {}

    for alias, code in TEAM_ALIASES.items():
        idx = lowered.find(alias)
        if idx >= 0 and (code not in hits or idx < hits[code]):
            hits[code] = idx
    for code in set(TEAM_ALIASES.values()):
        m = re.search(rf"\b{code}\b", upper)
        if m and (code not in hits or m.start() < hits[code]):
            hits[code] = m.start()

    return [code for code, _ in sorted(hits.items(), key=lambda kv: kv[1])]


@dataclass
class Signal:
    """A tradeable disagreement between the model and a venue."""

    venue: str
    market_id: str
    title: str
    model_prob: float
    market_prob: float
    blended_prob: float
    edge: float
    ev_per_unit: float
    kelly_fraction: float
    suggested_stake: float
    price: float
    side: Side = Side.YES
    model_sd: float = 0.0
    matched_to: str = ""

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["side"] = self.side.value
        return d


class MarketRouter:
    """One place to ask "what is quoted, what do we think, and what is the edge"."""

    def __init__(self, venues: dict | None = None, store: TickStore | None = None,
                 risk: RiskGuard | None = None, bankroll: float = 1000.0,
                 model_weight: float = 0.35, kelly_fraction_used: float = 0.25):
        self.venues = venues or {}
        self.store = store or TickStore()
        self.risk = risk or RiskGuard(bankroll=bankroll)
        self.bankroll = bankroll
        self.model_weight = model_weight
        self.kelly_fraction = kelly_fraction_used
        self._books: dict[str, OrderBook] = {}
        self._poller: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------- discovery
    def list_markets(self, nfl_only: bool = True, limit: int = 300) -> list[Market]:
        out: list[Market] = []
        for name, client in self.venues.items():
            try:
                fn = getattr(client, "list_nfl_markets", None) if nfl_only else None
                out.extend(fn(limit) if fn else client.list_markets(limit=limit))
            except Exception as exc:  # noqa: BLE001
                log.info("%s market list unavailable: %s", name, exc)
        return out

    def venue_status(self) -> list[dict]:
        rows = []
        for name, client in self.venues.items():
            reachable, detail = True, "ok"
            try:
                client.list_markets(limit=1)
            except Exception as exc:  # noqa: BLE001
                reachable, detail = False, str(exc)[:200]
            rows.append({
                "venue": name,
                "authenticated": bool(getattr(client, "authenticated", False)),
                "reachable": reachable,
                "detail": detail,
            })
        return rows

    # ---------------------------------------------------------------- pricing
    def evaluate(self, market: Market, model_prob: float, model_sd: float = 0.0,
                 matched_to: str = "") -> Signal | None:
        """Price one contract against a model probability."""
        price = market.yes_ask if market.yes_ask is not None else market.mid
        if price is None or not (0.0 < price < 1.0):
            return None
        fee = VENUE_FEES.get(market.venue, 0.0)

        blended = float(blend_with_market(model_prob, price, self.model_weight))
        # Trade whichever side the blended view prefers.
        side, trade_price, trade_prob = Side.YES, price, blended
        no_price = 1.0 - (market.yes_bid if market.yes_bid is not None else price)
        if (1.0 - blended) - no_price > blended - price:
            side, trade_price, trade_prob = Side.NO, no_price, 1.0 - blended

        edge = compute_edge(trade_prob, trade_price, fee=fee)
        k = kelly_with_uncertainty(trade_prob, trade_price, prob_sd=max(model_sd, 0.02),
                                   fraction=self.kelly_fraction, fee=fee,
                                   cap=self.risk.limits.max_portfolio_fraction / 4)
        return Signal(
            venue=market.venue, market_id=market.market_id, title=market.title,
            model_prob=float(model_prob), market_prob=float(trade_price),
            blended_prob=float(trade_prob), edge=float(edge.edge),
            ev_per_unit=float(edge.ev_per_unit), kelly_fraction=float(k),
            suggested_stake=float(k * self.bankroll), price=float(trade_price),
            side=side, model_sd=model_sd, matched_to=matched_to,
        )

    def scan(self, book, markets: list[Market] | None = None,
             min_edge: float | None = None) -> list[Signal]:
        """Score every quoted market the model can price.

        `book` is a :class:`~gridiron.exchange.matching.ProbabilityBook`, which
        prices a contract from its title: the teams involved, the kind of bet
        and the line. Anything it cannot classify is skipped rather than guessed
        at -- a totals contract priced off a moneyline probability is a real
        bet on the wrong question.
        """
        markets = markets if markets is not None else self.list_markets()
        min_edge = self.risk.limits.min_edge if min_edge is None else min_edge
        signals: list[Signal] = []
        for m in markets:
            teams = extract_teams(f"{m.title} {m.ticker}")
            priced = book.price(m.title, m.ticker, teams)
            if priced is None:
                continue
            prob, description, game_id = priced
            # Monte Carlo standard error plus a floor for model error itself.
            n = max(getattr(book.find_game(teams), "sample_size", lambda: 5000)(), 1)
            sd = float(np.sqrt(max(prob * (1 - prob) / n, 1e-9))) + 0.03
            sig = self.evaluate(m, prob, sd, matched_to=description)
            if sig and sig.edge >= min_edge:
                signals.append(sig)
                self.store.record_signal(sig.venue, sig.market_id, sig.title, sig.as_dict())
        return sorted(signals, key=lambda s: -s.edge)

    @staticmethod
    def match_key(market: Market, model_probs: dict[str, float]) -> str | None:
        """Direct id/ticker lookup, for callers holding a plain probability map."""
        if market.market_id in model_probs:
            return market.market_id
        if market.ticker in model_probs:
            return market.ticker
        for team in extract_teams(f"{market.title} {market.ticker}"):
            if team in model_probs:
                return team
        return None

    # --------------------------------------------------------------- polling
    def poll_once(self, markets: list[Market]) -> int:
        """Snapshot every book once and write microstructure features."""
        rows = []
        now = time.time()
        for m in markets:
            client = self.venues.get(m.venue)
            if client is None:
                continue
            try:
                book = client.get_orderbook(m.market_id)
            except Exception as exc:  # noqa: BLE001
                log.debug("book fetch failed for %s: %s", m.market_id, exc)
                continue
            prev = self._books.get(m.market_id)
            feats = book_features(book, prev)
            self._books[m.market_id] = book
            rows.append((now, m.venue, m.market_id, feats.get("best_bid"),
                         feats.get("best_ask"), feats.get("mid"), feats.get("micro_price"),
                         m.last_price, feats.get("bid_depth"), feats.get("ask_depth"),
                         feats.get("imbalance"), feats.get("ofi"), m.volume))
        self.store.record_ticks(rows)
        return len(rows)

    def start_polling(self, markets: list[Market], interval: float = 5.0) -> None:
        """Background quote poller for the live panel."""
        if self._poller and self._poller.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                try:
                    self.poll_once(markets)
                except Exception as exc:  # noqa: BLE001
                    log.warning("poll failed: %s", exc)
                self._stop.wait(interval)

        self._poller = threading.Thread(target=loop, name="gridiron-poller", daemon=True)
        self._poller.start()

    def stop_polling(self) -> None:
        self._stop.set()

    @property
    def polling(self) -> bool:
        return bool(self._poller and self._poller.is_alive())

    # --------------------------------------------------------------- trading
    def submit(self, signal: Signal, quantity: float | None = None,
               broker=None, confirmed: bool = False,
               order_type: OrderType = OrderType.LIMIT) -> dict:
        """Route an order through the risk guard.

        Paper is the default execution path. A live order additionally requires
        ``GRIDIRON_TRADING_MODE=live`` *and* an explicit confirmation from the
        caller, so nothing reaches an exchange by accident.
        """
        qty = quantity if quantity is not None else max(
            int(signal.suggested_stake / max(signal.price, 0.01)), 0)
        if qty <= 0:
            return {"submitted": False, "reason": "computed size is zero"}

        order = Order(venue=signal.venue, market_id=signal.market_id, side=signal.side,
                      action=Action.BUY, quantity=qty, price=signal.price,
                      order_type=order_type, note=signal.title)

        positions = broker.positions() if broker else []
        decision = self.risk.check(order, positions, edge=signal.edge, confirmed=confirmed)
        if not decision.allowed:
            return {"submitted": False, "reason": decision.reason, "order": order.as_dict()}
        if decision.adjusted_quantity is not None:
            order.quantity = decision.adjusted_quantity

        if broker is None:
            return {"submitted": False, "reason": "no broker configured",
                    "order": order.as_dict()}

        placed = broker.place_order(order)
        self.risk.record(placed)
        self.store.record_trade(self.risk.mode.value, placed, signal.model_prob,
                                {"signal": signal.as_dict(), "risk": decision.as_dict()})
        return {"submitted": True, "order": placed.as_dict(),
                "mode": self.risk.mode.value, "reason": decision.reason}
