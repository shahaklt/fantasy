"""Venue-agnostic market, order and position types, plus a paper broker.

Every venue is normalised to the same shape: a binary contract quoted in
probability units (0-1), so a Kalshi price in cents and a Polymarket price in
USDC land in the same table and the same risk calculation.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Protocol

from ..quant.microstructure import BookLevel, OrderBook


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


@dataclass
class Market:
    """A binary contract, normalised across venues."""

    venue: str
    market_id: str
    ticker: str
    title: str
    event_id: str = ""
    yes_bid: float | None = None      # probability units
    yes_ask: float | None = None
    last_price: float | None = None
    volume: float = 0.0
    open_interest: float = 0.0
    close_time: str = ""
    status: str = "open"
    metadata: dict = field(default_factory=dict)

    @property
    def mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return self.last_price
        return (self.yes_bid + self.yes_ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    def as_dict(self) -> dict:
        d = asdict(self)
        d["mid"] = self.mid
        d["spread"] = self.spread
        return d


@dataclass
class Order:
    venue: str
    market_id: str
    side: Side
    action: Action
    quantity: float
    price: float                     # probability units
    order_type: OrderType = OrderType.LIMIT
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    client_id: str = ""
    status: str = "pending"
    filled: float = 0.0
    avg_fill_price: float = 0.0
    created_at: float = field(default_factory=time.time)
    note: str = ""

    @property
    def notional(self) -> float:
        """Cash at risk: buying a contract costs price x quantity."""
        return self.quantity * (self.price if self.action == Action.BUY else (1.0 - self.price))

    def as_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value if isinstance(self.side, Side) else self.side
        d["action"] = self.action.value if isinstance(self.action, Action) else self.action
        d["order_type"] = self.order_type.value if isinstance(self.order_type, OrderType) else self.order_type
        d["notional"] = self.notional
        return d


@dataclass
class Position:
    venue: str
    market_id: str
    ticker: str
    side: Side
    quantity: float
    avg_price: float
    realised_pnl: float = 0.0

    def unrealised_pnl(self, mark: float | None) -> float:
        if mark is None:
            return 0.0
        value = mark if self.side == Side.YES else (1.0 - mark)
        return (value - self.avg_price) * self.quantity

    def as_dict(self, mark: float | None = None) -> dict:
        d = asdict(self)
        d["side"] = self.side.value if isinstance(self.side, Side) else self.side
        d["unrealised_pnl"] = self.unrealised_pnl(mark)
        d["mark"] = mark
        return d


class Broker(Protocol):
    """Minimal interface every venue adapter implements."""

    venue: str

    def list_markets(self, query: str | None = None, limit: int = 200) -> list[Market]: ...
    def get_market(self, market_id: str) -> Market | None: ...
    def get_orderbook(self, market_id: str, depth: int = 10) -> OrderBook: ...
    def place_order(self, order: Order) -> Order: ...
    def cancel_order(self, order_id: str) -> bool: ...
    def positions(self) -> list[Position]: ...
    def balance(self) -> float: ...


class PaperBroker:
    """Simulated venue that fills against a real order book.

    This is the default execution path. It marks positions off live quotes but
    never sends anything to an exchange, so strategies can be run end-to-end --
    including the sizing and risk logic -- before any money is involved.
    """

    venue = "paper"

    def __init__(self, quote_source: "Broker | None" = None, starting_cash: float = 1000.0,
                 fee: float = 0.02, slippage: float = 0.005):
        self.quotes = quote_source
        self.cash = float(starting_cash)
        self.starting_cash = float(starting_cash)
        self.fee = fee
        self.slippage = slippage
        self._positions: dict[tuple[str, str], Position] = {}
        self.orders: list[Order] = []
        self.fills: list[dict] = []

    # -- market data passes through to the real venue when one is attached ----
    def list_markets(self, query: str | None = None, limit: int = 200) -> list[Market]:
        return self.quotes.list_markets(query, limit) if self.quotes else []

    def get_market(self, market_id: str) -> Market | None:
        return self.quotes.get_market(market_id) if self.quotes else None

    def get_orderbook(self, market_id: str, depth: int = 10) -> OrderBook:
        return self.quotes.get_orderbook(market_id, depth) if self.quotes else OrderBook()

    # -- execution ------------------------------------------------------------
    def place_order(self, order: Order) -> Order:
        book = self.get_orderbook(order.market_id)
        fill_price = order.price
        if order.order_type == OrderType.MARKET:
            side = "buy" if order.action == Action.BUY else "sell"
            swept = book.sweep_cost(order.quantity, side)
            if swept is not None:
                fill_price = swept
            fill_price += self.slippage if order.action == Action.BUY else -self.slippage
        fill_price = float(min(max(fill_price, 0.01), 0.99))

        cost = order.quantity * (fill_price if order.action == Action.BUY else (1 - fill_price))
        if order.action == Action.BUY and cost > self.cash:
            order.status = "rejected"
            order.note = "insufficient paper cash"
            self.orders.append(order)
            return order

        self.cash -= cost
        order.status = "filled"
        order.filled = order.quantity
        order.avg_fill_price = fill_price
        self.orders.append(order)
        self.fills.append({"order_id": order.order_id, "market_id": order.market_id,
                           "price": fill_price, "quantity": order.quantity,
                           "side": order.side.value, "ts": time.time()})

        key = (order.market_id, order.side.value)
        pos = self._positions.get(key)
        if pos is None:
            self._positions[key] = Position(
                venue=self.venue, market_id=order.market_id, ticker=order.market_id,
                side=order.side, quantity=order.quantity, avg_price=fill_price)
        else:
            total = pos.quantity + order.quantity
            pos.avg_price = (pos.avg_price * pos.quantity + fill_price * order.quantity) / max(total, 1e-9)
            pos.quantity = total
        return order

    def cancel_order(self, order_id: str) -> bool:
        for o in self.orders:
            if o.order_id == order_id and o.status == "pending":
                o.status = "cancelled"
                return True
        return False

    def positions(self) -> list[Position]:
        return list(self._positions.values())

    def balance(self) -> float:
        return self.cash

    def equity(self, marks: dict[str, float] | None = None) -> float:
        marks = marks or {}
        value = self.cash
        for pos in self._positions.values():
            mark = marks.get(pos.market_id)
            if mark is None:
                m = self.get_market(pos.market_id)
                mark = m.mid if m else pos.avg_price
            price = mark if pos.side == Side.YES else (1.0 - (mark or pos.avg_price))
            value += pos.quantity * (price or pos.avg_price)
        return float(value)
