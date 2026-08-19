"""Risk guard: the checks every order passes before it can reach a venue.

Live trading is off unless it is switched on deliberately, and even then every
order is bounded by per-order, per-market and daily limits. A kill-switch file
stops everything without touching the code -- create it and the next order is
refused.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import USER_DIR
from .base import Action, Order, Position, TradingMode

log = logging.getLogger(__name__)

KILL_SWITCH = USER_DIR / "STOP_TRADING"
RISK_STATE_PATH = USER_DIR / "risk_state.json"


@dataclass
class RiskLimits:
    """Hard bounds on trading activity. All amounts are in account currency."""

    max_order_notional: float = 25.0
    max_market_notional: float = 100.0
    max_daily_notional: float = 250.0
    max_open_positions: int = 20
    max_portfolio_fraction: float = 0.25    # of starting bankroll, gross
    min_edge: float = 0.03                  # skip anything thinner than this
    min_price: float = 0.03                 # never trade lottery tickets
    max_price: float = 0.97
    require_confirmation: bool = True       # live orders need an explicit token

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""
    adjusted_quantity: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class RiskGuard:
    """Stateful pre-trade checks with a persistent daily notional counter."""

    def __init__(self, limits: RiskLimits | None = None, bankroll: float = 1000.0,
                 mode: TradingMode | None = None):
        self.limits = limits or RiskLimits()
        self.bankroll = float(bankroll)
        self.mode = mode or self._mode_from_env()
        self._state = self._load_state()

    @staticmethod
    def _mode_from_env() -> TradingMode:
        """Live trading requires an explicit opt-in in the environment."""
        raw = os.environ.get("GRIDIRON_TRADING_MODE", "paper").strip().lower()
        return TradingMode.LIVE if raw == "live" else TradingMode.PAPER

    # ------------------------------------------------------------------ state
    def _load_state(self) -> dict:
        today = time.strftime("%Y-%m-%d")
        if RISK_STATE_PATH.exists():
            try:
                st = json.loads(RISK_STATE_PATH.read_text())
                if st.get("date") == today:
                    return st
            except Exception:  # noqa: BLE001
                pass
        return {"date": today, "daily_notional": 0.0, "orders": 0}

    def _save_state(self) -> None:
        try:
            RISK_STATE_PATH.write_text(json.dumps(self._state))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not persist risk state: %s", exc)

    @property
    def daily_notional(self) -> float:
        return float(self._state.get("daily_notional", 0.0))

    def kill_switch_active(self) -> bool:
        return KILL_SWITCH.exists()

    # ------------------------------------------------------------------ checks
    def check(self, order: Order, positions: list[Position] | None = None,
              edge: float | None = None, confirmed: bool = False) -> RiskDecision:
        """Validate one order. Returns a decision, never raises on a rejection."""
        positions = positions or []

        if self.kill_switch_active():
            return RiskDecision(False, f"kill switch active ({KILL_SWITCH})")

        if not (self.limits.min_price <= order.price <= self.limits.max_price):
            return RiskDecision(False, f"price {order.price:.3f} outside "
                                       f"[{self.limits.min_price}, {self.limits.max_price}]")

        if edge is not None and edge < self.limits.min_edge:
            return RiskDecision(False, f"edge {edge:.3f} below minimum {self.limits.min_edge}")

        if self.mode == TradingMode.LIVE and self.limits.require_confirmation and not confirmed:
            return RiskDecision(False, "live order requires explicit confirmation")

        notional = order.notional
        if notional > self.limits.max_order_notional:
            scale = self.limits.max_order_notional / max(notional, 1e-9)
            adjusted = max(int(order.quantity * scale), 0)
            if adjusted <= 0:
                return RiskDecision(False, f"order notional {notional:.2f} exceeds "
                                           f"{self.limits.max_order_notional:.2f}")
            return RiskDecision(True, f"quantity reduced to respect per-order limit",
                                adjusted_quantity=adjusted)

        existing = sum(p.quantity * p.avg_price for p in positions
                       if p.market_id == order.market_id)
        if existing + notional > self.limits.max_market_notional:
            return RiskDecision(False, f"market exposure {existing + notional:.2f} exceeds "
                                       f"{self.limits.max_market_notional:.2f}")

        if self.daily_notional + notional > self.limits.max_daily_notional:
            return RiskDecision(False, f"daily notional {self.daily_notional + notional:.2f} "
                                       f"exceeds {self.limits.max_daily_notional:.2f}")

        if order.action == Action.BUY and len(positions) >= self.limits.max_open_positions:
            if not any(p.market_id == order.market_id for p in positions):
                return RiskDecision(False, f"already holding {len(positions)} positions")

        gross = sum(p.quantity * p.avg_price for p in positions) + notional
        if gross > self.bankroll * self.limits.max_portfolio_fraction:
            return RiskDecision(False, f"gross exposure {gross:.2f} exceeds "
                                       f"{self.limits.max_portfolio_fraction:.0%} of bankroll")

        return RiskDecision(True, "ok")

    def record(self, order: Order) -> None:
        self._state["daily_notional"] = self.daily_notional + order.notional
        self._state["orders"] = int(self._state.get("orders", 0)) + 1
        self._save_state()

    def status(self) -> dict:
        return {
            "mode": self.mode.value,
            "kill_switch": self.kill_switch_active(),
            "kill_switch_path": str(KILL_SWITCH),
            "bankroll": self.bankroll,
            "daily_notional": self.daily_notional,
            "orders_today": int(self._state.get("orders", 0)),
            "limits": self.limits.as_dict(),
        }
