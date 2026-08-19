"""Request/response models for the HTTP API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LeagueUpdate(BaseModel):
    name: str | None = None
    teams: int | None = Field(default=None, ge=2, le=32)
    scoring_preset: str | None = None
    scoring: dict[str, float] | None = None
    roster: dict[str, int] | None = None
    draft_type: str | None = None
    draft_slot: int | None = Field(default=None, ge=1, le=32)
    auction_budget: int | None = Field(default=None, ge=1)
    playoff_teams: int | None = Field(default=None, ge=2)
    regular_season_weeks: int | None = Field(default=None, ge=1, le=18)


class RebuildRequest(BaseModel):
    n_sims: int = Field(default=5000, ge=200, le=200_000)
    season: int | None = None
    refresh_data: bool = False


class DraftPick(BaseModel):
    player_id: str
    team_slot: int | None = None


class DraftReset(BaseModel):
    my_slot: int | None = None
    keep_picks: bool = False


class RecommendRequest(BaseModel):
    n_sims: int = Field(default=150, ge=20, le=3000)
    top_k: int = Field(default=12, ge=1, le=40)
    candidates: list[str] | None = None


class LineupRequest(BaseModel):
    player_ids: list[str]
    week: int = 1


class LeagueSimRequest(BaseModel):
    teams: dict[str, list[str]]
    regular_season_weeks: int | None = None


class TradeRequest(BaseModel):
    venue: str
    market_id: str
    side: str = "yes"
    action: str = "buy"
    quantity: float | None = None
    price: float | None = None
    order_type: str = "limit"
    model_prob: float | None = None
    confirm: bool = False
    live: bool = False


class SignalRequest(BaseModel):
    week: int | None = None
    min_edge: float = 0.02
    n_sims: int = Field(default=8000, ge=500, le=100_000)


class PollRequest(BaseModel):
    interval: float = Field(default=5.0, ge=1.0, le=300.0)
    limit: int = Field(default=60, ge=1, le=500)


class CredentialsRequest(BaseModel):
    venue: str
    key_id: str | None = None
    private_key_path: str | None = None


class ApiResponse(BaseModel):
    ok: bool = True
    detail: str = ""
    data: Any = None
