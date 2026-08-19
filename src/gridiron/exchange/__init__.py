"""Prediction-market venues, execution and risk.

Paper trading is the default execution path; live orders require an explicit
environment opt-in, configured credentials and a per-order confirmation.
"""
from .base import (Action, Broker, Market, Order, OrderType, PaperBroker, Position,
                   Side, TradingMode)
from .demo import DemoVenue, demo_enabled
from .kalshi import KalshiClient, KalshiCredentials
from .matching import GameProbabilities, ProbabilityBook, classify_market
from .polymarket import PolymarketClient, PolymarketCredentials
from .risk import RiskDecision, RiskGuard, RiskLimits
from .router import MarketRouter, Signal, extract_teams
from .store import TickStore

__all__ = [
    "Action", "Broker", "Market", "Order", "OrderType", "PaperBroker", "Position",
    "Side", "TradingMode", "DemoVenue", "demo_enabled", "KalshiClient",
    "KalshiCredentials", "GameProbabilities", "ProbabilityBook", "classify_market",
    "PolymarketClient", "PolymarketCredentials", "RiskDecision", "RiskGuard",
    "RiskLimits", "MarketRouter", "Signal", "extract_teams", "TickStore",
]
