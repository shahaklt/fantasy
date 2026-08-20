"""FastAPI application: the whole engine behind one local HTTP server.

Runs entirely on your machine. The only outbound calls are to the public data
sources (nflverse, FantasyPros mirror) and, if you configure them, the trading
venues. Nothing is uploaded anywhere.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import numpy as np
import polars as pl
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import net, pipeline
from ..analysis import GameAnalyst
from ..analysis.compare import (accuracy_scorecard, agreement_stats, disagreements,
                                roster_report, season_comparison, weekly_comparison)
from ..data.espn import (EspnCredentials, EspnLeague, espn_available,
                         load_roster, save_roster)
from ..config import REPO_ROOT, current_season, detect_backend
from ..draft import DraftSimulator, DraftState, positional_scarcity
from ..exchange.base import Action, OrderType, PaperBroker, Side, TradingMode
from ..exchange.demo import DemoVenue, demo_enabled
from ..exchange.kalshi import KalshiClient
from ..exchange.polymarket import PolymarketClient
from ..exchange.matching import ProbabilityBook
from ..exchange.risk import RiskGuard, RiskLimits
from ..exchange.router import MarketRouter, extract_teams
from ..exchange.store import TickStore
from ..quant.microstructure import price_momentum, realised_volatility
from ..scheduler import build_default_scheduler
from ..scoring import LeagueSettings
from ..sim.league import FantasyTeam, LeagueSimulator, start_sit
from . import access, lan
from .schemas import (CredentialsRequest, DraftPick, DraftReset, EspnCompareRequest,
                      EspnCredentialsRequest, EspnScoreRequest, EspnSyncRequest,
                      EspnTeamRequest,
                      LeagueSimRequest,
                      LeagueUpdate, LineupRequest, PollRequest, RebuildRequest,
                      RecommendRequest, SignalRequest, TradeRequest)

log = logging.getLogger(__name__)

WEB_DIR = REPO_ROOT / "web"

app = FastAPI(title="Gridiron", version="1.0.0",
              description="Monte Carlo fantasy football and prediction-market engine")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(access.AccessGuard)


# --------------------------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------------------------
class AppState:
    def __init__(self):
        self.draft = DraftState(league=pipeline.load_league(), my_slot=1)
        self.store = TickStore()
        self.venues = {"kalshi": KalshiClient(), "polymarket": PolymarketClient()}
        if demo_enabled():
            # A synthetic venue so the live tape and signal path can be exercised
            # without credentials or a network connection.
            self.venues["demo"] = DemoVenue()
        self.risk = RiskGuard(limits=RiskLimits(), bankroll=1000.0)
        self.router = MarketRouter(venues=self.venues, store=self.store, risk=self.risk)
        self.paper = PaperBroker(quote_source=None, starting_cash=1000.0)
        self.scheduler = build_default_scheduler(router=self.router)
        self.game_cache: dict[int, tuple] = {}
        self.model_probs: dict[str, float] = {}
        self.watchlist: list = []
        self._espn: EspnLeague | None = None
        # The synced roster is read back from disk, so a restart does not
        # silently empty My League and start/sit.
        saved = load_roster()
        self.espn_roster: list[str] = saved["player_ids"]   # player_ids, from ESPN
        self.espn_sync: dict = ({"imported": {"roster": {"team": saved["team"]}}}
                                if saved["player_ids"] else {})

    def remember_roster(self, player_ids: list[str], team: dict | None) -> None:
        self.espn_roster = player_ids
        self.espn_sync.setdefault("imported", {})["roster"] = {
            **self.espn_sync.get("imported", {}).get("roster", {}), "team": team}
        save_roster(player_ids, team)

    def espn(self, refresh: bool = False) -> EspnLeague:
        if self._espn is None or refresh:
            self._espn = EspnLeague()
        return self._espn

    def broker_for(self, venue: str, live: bool):
        if not live or self.risk.mode != TradingMode.LIVE:
            self.paper.quotes = self.venues.get(venue)
            return self.paper
        return self.venues.get(venue)


state = AppState()


def _df(frame: pl.DataFrame, limit: int | None = None) -> list[dict]:
    if frame is None or frame.is_empty():
        return []
    f = frame.head(limit) if limit else frame
    return json.loads(f.write_json())


def _clean(obj):
    """Make numpy/NaN values JSON-safe."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    # bool must be checked before int -- in Python bool *is* an int, and a
    # JSON API that answers 1 instead of true for a flag is a nuisance to use.
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return None if (np.isnan(v) or np.isinf(v)) else v
    if isinstance(obj, (np.integer, int)):
        return int(obj)
    if isinstance(obj, np.ndarray):
        return _clean(obj.tolist())
    return obj


def arts(build: bool = True, need_sims: bool = False):
    """Current artifacts, or a 503 that says exactly what is missing."""
    a = pipeline.get_artifacts(build_if_missing=build)
    st = pipeline.build_state()
    if not a.ready:
        if st.get("building"):
            raise HTTPException(503, f"first simulation is running (started {st.get('started')}); "
                                     "this takes about a minute")
        if st.get("error"):
            raise HTTPException(503, f"the last build failed: {st['error']}")
        raise HTTPException(503, "no projections yet — press Rebuild sims")
    if need_sims and (a.season_result is None or a.engine is None):
        pipeline.start_background_build()
        raise HTTPException(503, "this view needs the full simulation, which is rebuilding now; "
                                 "the draft board and projections work in the meantime")
    return a


# --------------------------------------------------------------------------------------
# Status & configuration
# --------------------------------------------------------------------------------------
@app.get("/api/status")
def status():
    a = pipeline.get_artifacts(build_if_missing=False)
    backend = detect_backend()
    return _clean({
        "season": current_season(),
        "compute": {"backend": backend.describe(), "gpu": backend.is_gpu,
                    "cpu_count": backend.cpu_count},
        "artifacts_ready": a.ready,
        "sims_ready": a.season_result is not None,
        "build": pipeline.build_state(),
        "meta": a.meta,
        "league": a.league.to_dict(),
        "scheduler": state.scheduler.status(),
        "trading_mode": state.risk.mode.value,
        "risk": state.risk.status(),
        "venues": [{"venue": name,
                    "authenticated": bool(getattr(c, "authenticated", False))}
                   for name, c in state.venues.items()],
        "polling": state.router.polling,
    })


# --------------------------------------------------------------------------------------
# Phone access
# --------------------------------------------------------------------------------------
def _local_only(request: Request):
    """Pairing material is readable from this machine and nowhere else.

    Handing the token back over the network would defeat the point of having
    one, so a paired phone can see *that* the gate is on but never its key.
    """
    if not access.is_loopback_client(request.scope):
        raise HTTPException(status_code=403,
                            detail="pairing details are only readable on the machine "
                                   "running the server")


def _on_the_network() -> bool:
    """Whether anything other than this machine can currently reach the panel."""
    return lan.listener.active or net.is_wildcard_host(access.settings.host)


@app.get("/api/access")
def access_info(request: Request):
    """What a phone needs to reach this panel, for the Settings screen."""
    local = access.is_loopback_client(request.scope)
    guarded = access.settings.required
    token = access.access_token(create=guarded) if local else None
    port = access.settings.pairing_port
    # A wildcard bind advertises a routable address; the on-demand listener is
    # always on every interface, whatever the main server bound to.
    advertise = "0.0.0.0" if lan.listener.active else access.settings.host
    pair = net.pairing(advertise, port, token, guarded)
    return _clean({
        "required": guarded,
        "local": local,
        "host": access.settings.host,
        "port": port,
        "on_network": _on_the_network(),
        # Only a loopback-bound server can be toggled: an interface the process
        # bound at startup cannot be given back without restarting it.
        "can_toggle": not net.is_wildcard_host(access.settings.host),
        "listener_port": lan.listener.port,
        # Always the real network interfaces, not the bind address: on a
        # loopback bind the useful answer is still "here is where you would be
        # reachable if you restarted with --lan".
        "lan_addresses": net.lan_addresses(),
        "url": pair.url if local else None,
        "alternates": pair.alternates() if local else [],
        "token": token,
        "qr": net.qr_svg(pair.url) if local else None,
        "qr_available": net.qr_svg("probe") is not None,
        "tunnel_available": net.tunnel_command() is not None,
        "pinned_by_env": bool(os.environ.get("GRIDIRON_TOKEN")),
    })


@app.post("/api/access/rotate")
def access_rotate(request: Request):
    """New token, every paired device logged out."""
    _local_only(request)
    try:
        token = access.rotate_token()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    pair = net.pairing("0.0.0.0" if lan.listener.active else access.settings.host,
                       access.settings.pairing_port, token, access.settings.required)
    return _clean({"token": token, "url": pair.url, "qr": net.qr_svg(pair.url),
                   "alternates": pair.alternates()})


@app.post("/api/access/enable")
def access_enable(request: Request):
    """Put the panel on the wifi, now, without losing the warm caches.

    The gate goes up *before* the socket opens, so there is never a moment
    where the network can reach an unguarded panel.
    """
    _local_only(request)
    if net.is_wildcard_host(access.settings.host):
        return _clean({"already": True, "detail": "the server was started with --lan; "
                                                  "it is already on the network"})

    access.settings.required = True
    token = access.access_token()
    try:
        port = lan.listener.start(app, access.settings.port)
    except Exception as exc:  # noqa: BLE001
        access.settings.required = False
        raise HTTPException(500, f"could not open a network listener: {exc}") from exc

    access.settings.lan_port = port
    pair = net.pairing("0.0.0.0", port, token, True)
    return _clean({"enabled": True, "port": port, "url": pair.url, "token": token,
                   "qr": net.qr_svg(pair.url), "alternates": pair.alternates()})


@app.post("/api/access/disable")
def access_disable(request: Request):
    """Take it back off the network. Paired devices stop resolving immediately."""
    _local_only(request)
    if net.is_wildcard_host(access.settings.host):
        raise HTTPException(400, "the server was started with --lan; restart it without "
                                 "that flag to take the panel off the network")
    lan.listener.stop()
    access.settings.lan_port = None
    access.settings.required = False
    return _clean({"enabled": False})


@app.get("/api/league")
def get_league():
    """Saved settings, which are the source of truth.

    The cached artifacts carry the league they were *built* with, so reading
    them here would keep serving the old scoring until the next rebuild.
    """
    league = pipeline.load_league()
    cached = pipeline.get_artifacts(build_if_missing=False)
    body = league.to_dict()
    body["applied_to_projections"] = bool(cached.ready and cached.league.to_dict() == body)
    return _clean(body)


@app.post("/api/league")
def update_league(update: LeagueUpdate):
    # `to_config` stores scoring only where it deviates from the preset, so the
    # preset stays authoritative and a custom tweak still survives a reload.
    current = pipeline.load_league().to_config()
    patch = {k: v for k, v in update.model_dump().items() if v is not None}
    if patch.get("scoring"):
        current.setdefault("scoring", {}).update(patch["scoring"])
    patch.pop("scoring", None)
    current.update(patch)
    league = LeagueSettings.from_dict(current)
    pipeline.save_league(league)
    pipeline.invalidate()
    state.draft = DraftState(league=league, my_slot=league.draft_slot)
    return _clean({"ok": True, "league": league.to_dict(),
                   "detail": "settings saved; rebuild projections to apply"})


@app.post("/api/rebuild")
def rebuild(req: RebuildRequest):
    """Start a rebuild on a worker thread and return immediately."""
    if req.refresh_data:
        pipeline.refresh_data(force=True)
    state.game_cache.clear()
    started = pipeline.start_background_build(league=pipeline.load_league(),
                                              season=req.season, n_sims=req.n_sims)
    return _clean({"ok": True, "started": started,
                   "detail": "rebuilding in the background" if started
                             else "a build is already running",
                   "build": pipeline.build_state()})


@app.get("/api/build")
def build_status():
    a = pipeline.get_artifacts(build_if_missing=False)
    return _clean({**pipeline.build_state(), "artifacts_ready": a.ready,
                   "sims_ready": a.season_result is not None, "meta": a.meta})


@app.post("/api/refresh")
def refresh():
    return _clean(pipeline.refresh_data(force=True))


# --------------------------------------------------------------------------------------
# Projections
# --------------------------------------------------------------------------------------
@app.get("/api/players")
def players(position: str | None = None, team: str | None = None,
            search: str | None = None, limit: int = Query(300, le=2000),
            sort: str = "proj_points", descending: bool = True):
    board = arts().board
    if position and position.upper() != "ALL":
        board = board.filter(pl.col("position") == position.upper())
    if team and team.upper() != "ALL":
        board = board.filter(pl.col("team") == team.upper())
    if search:
        board = board.filter(pl.col("player_name").str.to_lowercase().str.contains(search.lower()))
    if sort in board.columns:
        board = board.sort(sort, descending=descending, nulls_last=True)
    return _clean({"count": board.height, "players": _df(board, limit)})


@app.get("/api/players/{player_id}")
def player_detail(player_id: str):
    a = arts()
    row = a.board.filter(pl.col("player_id") == player_id)
    if row.is_empty():
        raise HTTPException(404, "player not found")
    detail = row.to_dicts()[0]

    res = a.season_result
    if res is not None and player_id in res.player_ids:
        i = res.player_ids.index(player_id)
        totals = res.totals[:, i]
        counts, edges = np.histogram(totals, bins=24)
        detail["distribution"] = {"counts": counts.tolist(), "edges": edges.tolist()}
        if res.weekly is not None:
            weekly = res.weekly[:, :, i]
            detail["weekly"] = [
                {"week": int(w), "mean": float(weekly[k].mean()),
                 "p10": float(np.percentile(weekly[k], 10)),
                 "p90": float(np.percentile(weekly[k], 90))}
                for k, w in enumerate(res.weeks)
            ]
    proj = a.projections.players.filter(pl.col("player_id") == player_id)
    if not proj.is_empty():
        detail["projection_inputs"] = proj.to_dicts()[0]
    return _clean(detail)


# --------------------------------------------------------------------------------------
# Draft room
# --------------------------------------------------------------------------------------
@app.get("/api/draft/board")
def draft_board(limit: int = Query(250, le=2000), position: str | None = None):
    a = arts()
    drafted = set(state.draft.drafted)
    board = a.board.filter(~pl.col("player_id").is_in(list(drafted))) if drafted else a.board
    if position and position.upper() != "ALL":
        board = board.filter(pl.col("position") == position.upper())
    return _clean({
        "pick": state.draft.pick_number,
        "on_the_clock": state.draft.team_on_clock(),
        "my_slot": state.draft.my_slot,
        "my_next_picks": state.draft.my_next_picks(5),
        "drafted_count": len(drafted),
        "players": _df(board.sort("vorp", descending=True, nulls_last=True), limit),
    })


@app.get("/api/draft/state")
def draft_state():
    a = arts()
    names = {r["player_id"]: r for r in a.board.select(
        ["player_id", "player_name", "position", "team", "proj_points", "vorp"]).to_dicts()}
    return _clean({
        "pick": state.draft.pick_number,
        "on_the_clock": state.draft.team_on_clock(),
        "my_slot": state.draft.my_slot,
        "my_next_picks": state.draft.my_next_picks(6),
        "picks": [{"pick": i + 1, "player": names.get(pid, {"player_id": pid}),
                   "team_slot": _slot_for_pick(i + 1)}
                  for i, pid in enumerate(state.draft.drafted)],
        "rosters": {str(k): [names.get(p, {"player_id": p}) for p in v]
                    for k, v in state.draft.rosters.items()},
    })


def _slot_for_pick(pick: int) -> int:
    return state.draft.team_on_clock(pick)


@app.post("/api/draft/pick")
def draft_pick(pick: DraftPick):
    a = arts()
    if a.board.filter(pl.col("player_id") == pick.player_id).is_empty():
        raise HTTPException(404, "unknown player")
    if pick.player_id in state.draft.drafted:
        raise HTTPException(400, "player already drafted")
    state.draft.record(pick.player_id, pick.team_slot)
    return draft_state()


@app.post("/api/draft/undo")
def draft_undo():
    state.draft.undo()
    return draft_state()


@app.post("/api/draft/reset")
def draft_reset(req: DraftReset):
    league = pipeline.load_league()
    slot = req.my_slot or state.draft.my_slot
    state.draft = DraftState(league=league, my_slot=slot)
    return draft_state()


@app.post("/api/draft/recommend")
def draft_recommend(req: RecommendRequest):
    a = arts()
    sim = DraftSimulator(a.board, a.league, seed=int(time.time()) % 10_000)
    rec = sim.evaluate_candidates(state.draft, candidates=req.candidates,
                                  n_sims=req.n_sims, top_k=req.top_k)
    scarcity = positional_scarcity(a.board, a.league, set(state.draft.drafted))
    return _clean({
        "pick": state.draft.pick_number,
        "my_next_picks": state.draft.my_next_picks(5),
        "recommendations": _df(rec),
        "scarcity": _df(scarcity),
    })


@app.get("/api/draft/scarcity")
def draft_scarcity():
    a = arts()
    return _clean(_df(positional_scarcity(a.board, a.league, set(state.draft.drafted))))


# --------------------------------------------------------------------------------------
# Games
# --------------------------------------------------------------------------------------
def _games(week: int, n_sims: int = 8000):
    key = (week, n_sims)
    cached = state.game_cache.get(key)
    if cached:
        return cached
    a = arts(need_sims=True)
    analyst = GameAnalyst(a.engine)
    preds, players = analyst.predict_week(week, n_sims=n_sims)
    state.game_cache[key] = (analyst, preds, players)
    return state.game_cache[key]


@app.get("/api/games")
def games(week: int = 1, n_sims: int = Query(8000, ge=500, le=100_000)):
    _, preds, _ = _games(week, n_sims)
    return _clean({"week": week, "games": [p.summary() for p in preds]})


@app.get("/api/games/{game_id}")
def game_detail(game_id: str, week: int = 1, n_sims: int = Query(8000, ge=500, le=100_000)):
    analyst, preds, players = _games(week, n_sims)
    match = next((p for p in preds if p.game_id == game_id), None)
    if match is None:
        raise HTTPException(404, "game not found for that week")
    return _clean({
        "summary": match.summary(),
        "distribution": match.score_distribution(),
        "alt_lines": match.alt_lines(),
        "analytic": match.analytic_comparison(),
        "home_breakdown": analyst.team_breakdown(players, match.home_team),
        "away_breakdown": analyst.team_breakdown(players, match.away_team),
    })


@app.get("/api/stars")
def stars(week: int = 1, top_n: int = 25, n_sims: int = Query(8000, ge=500, le=100_000)):
    analyst, _, players = _games(week, n_sims)
    return _clean({"week": week, "players": _df(analyst.star_players(players, top_n))})


@app.get("/api/weekly")
def weekly_projections(week: int = 1, position: str | None = None,
                       limit: int = Query(200, le=1000),
                       n_sims: int = Query(8000, ge=500, le=100_000)):
    _, _, players = _games(week, n_sims)
    if position and position.upper() != "ALL":
        players = players.filter(pl.col("position") == position.upper())
    return _clean({"week": week, "players": _df(players, limit)})


# --------------------------------------------------------------------------------------
# Fantasy league
# --------------------------------------------------------------------------------------
@app.get("/api/myroster")
def my_roster():
    """Your roster, from ESPN when connected.

    Having imported it once, the app should not also ask you to type it in.
    """
    a = pipeline.get_artifacts(build_if_missing=False)
    ids = state.espn_roster
    rows = []
    if ids and a.ready:
        rows = _df(a.board.filter(pl.col("player_id").is_in(ids)))
    return _clean({"source": "espn" if ids else "manual",
                   "player_ids": ids, "players": rows,
                   "team": (state.espn_sync.get("imported", {})
                            .get("roster", {}) or {}).get("team")})


@app.delete("/api/espn/team")
def espn_forget_team():
    """Forget the saved team and roster, without touching the cookies."""
    creds = EspnCredentials.load()
    if creds is not None:
        creds.team_id = None
        creds.save()
    state.remember_roster([], None)
    state.espn_sync = {}
    return _clean({"forgotten": True})


@app.post("/api/lineup")
def lineup(req: LineupRequest):
    a = arts(need_sims=True)
    # An imported roster is the default subject when none is supplied.
    if not req.player_ids and state.espn_roster:
        req.player_ids = state.espn_roster
    if a.season_result is None or a.season_result.weekly is None:
        raise HTTPException(400, "weekly simulations are not loaded; rebuild first")
    return _clean(_df(start_sit(a.season_result, a.league, req.player_ids, req.week)))


@app.post("/api/league/simulate")
def simulate_league(req: LeagueSimRequest):
    a = arts(need_sims=True)
    if a.season_result is None or a.season_result.weekly is None:
        raise HTTPException(400, "weekly simulations are not loaded; rebuild first")
    sim = LeagueSimulator(a.season_result, a.league)
    teams = [FantasyTeam(name=name, player_ids=ids) for name, ids in req.teams.items()]
    if len(teams) < 2:
        raise HTTPException(400, "need at least two teams")
    result = sim.simulate(teams, regular_weeks=req.regular_season_weeks)
    return _clean({"standings": _df(result.summary())})


# --------------------------------------------------------------------------------------
# Markets / trading
# --------------------------------------------------------------------------------------
@app.get("/api/markets")
def markets(nfl_only: bool = True, limit: int = Query(200, le=1000)):
    found = state.router.list_markets(nfl_only=nfl_only, limit=limit)
    return _clean({
        "venues": state.router.venue_status(),
        "count": len(found),
        "markets": [m.as_dict() for m in found[:limit]],
    })


@app.post("/api/markets/signals")
def market_signals(req: SignalRequest):
    """Score live contracts against the simulator's own game probabilities."""
    week = req.week or 1
    _, preds, _ = _games(week, req.n_sims)
    book = ProbabilityBook.from_predictions(preds)

    found = state.router.list_markets(nfl_only=True)
    signals = state.router.scan(book, found, min_edge=req.min_edge)
    priced = sum(1 for m in found
                 if book.price(m.title, m.ticker, extract_teams(f"{m.title} {m.ticker}")))
    return _clean({
        "week": week,
        "markets_scanned": len(found),
        "markets_priced": priced,
        "venues": state.router.venue_status(),
        "signals": [s.as_dict() for s in signals],
    })


@app.get("/api/markets/{market_id}/ticks")
def market_ticks(market_id: str, minutes: int = 240):
    series = state.store.price_series(market_id, minutes)
    mids = np.array([r["mid"] for r in series if r["mid"] is not None], dtype=float)
    return _clean({
        "market_id": market_id,
        "ticks": series,
        "realised_vol": realised_volatility(mids) if len(mids) > 2 else 0.0,
        "momentum": price_momentum(mids) if len(mids) > 2 else 0.0,
    })


@app.get("/api/markets/activity")
def market_activity(minutes: int = 60):
    return _clean({"markets": state.store.recent_markets(minutes),
                   "signals": state.store.signals(50)})


@app.post("/api/markets/poll/start")
def poll_start(req: PollRequest):
    found = state.router.list_markets(nfl_only=True, limit=req.limit)
    if not found:
        raise HTTPException(503, "no venue is reachable; check your network and credentials")
    state.watchlist = found[: req.limit]
    state.router.start_polling(state.watchlist, interval=req.interval)
    return _clean({"ok": True, "polling": state.router.polling,
                   "markets": len(state.watchlist), "interval": req.interval})


@app.post("/api/markets/poll/stop")
def poll_stop():
    state.router.stop_polling()
    return {"ok": True, "polling": state.router.polling}


@app.post("/api/trade")
def trade(req: TradeRequest):
    """Submit an order. Paper unless live mode is enabled *and* confirmed."""
    from ..exchange.base import Order
    from ..exchange.router import Signal

    price = req.price
    market = None
    client = state.venues.get(req.venue)
    if client is not None and price is None:
        try:
            market = client.get_market(req.market_id)
            price = market.yes_ask if market and market.yes_ask else (market.mid if market else None)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"could not fetch a price for {req.market_id}: {exc}")
    if price is None:
        raise HTTPException(400, "no price supplied and none could be fetched")

    model_prob = req.model_prob if req.model_prob is not None else float(price)
    signal = Signal(venue=req.venue, market_id=req.market_id,
                    title=market.title if market else req.market_id,
                    model_prob=model_prob, market_prob=float(price),
                    blended_prob=model_prob, edge=model_prob - float(price),
                    ev_per_unit=0.0, kelly_fraction=0.0, suggested_stake=0.0,
                    price=float(price), side=Side(req.side))
    broker = state.broker_for(req.venue, req.live)
    if broker is None:
        raise HTTPException(400, f"unknown venue {req.venue}")

    result = state.router.submit(signal, quantity=req.quantity, broker=broker,
                                 confirmed=req.confirm,
                                 order_type=OrderType(req.order_type))
    result["live"] = bool(req.live and state.risk.mode == TradingMode.LIVE)
    return _clean(result)


@app.get("/api/portfolio")
def portfolio():
    positions = []
    for name, client in state.venues.items():
        if getattr(client, "authenticated", False):
            try:
                positions += [p.as_dict() for p in client.positions()]
            except Exception as exc:  # noqa: BLE001
                log.info("positions unavailable for %s: %s", name, exc)
    paper = [p.as_dict() for p in state.paper.positions()]
    return _clean({
        "mode": state.risk.mode.value,
        "paper": {"cash": state.paper.balance(), "equity": state.paper.equity(),
                  "positions": paper},
        "live_positions": positions,
        "trades": state.store.trades(100),
        "risk": state.risk.status(),
    })


@app.post("/api/credentials")
def save_credentials(req: CredentialsRequest):
    """Store a *reference* to local credentials. Key material is never uploaded."""
    if req.venue == "kalshi":
        from ..exchange.kalshi import KalshiCredentials
        if not req.key_id or not req.private_key_path:
            raise HTTPException(400, "key_id and private_key_path are both required")
        if not Path(req.private_key_path).exists():
            raise HTTPException(400, f"no such file: {req.private_key_path}")
        path = KalshiCredentials.save(req.key_id, req.private_key_path)
        state.venues["kalshi"] = KalshiClient()
        state.router.venues = state.venues
        return {"ok": True, "detail": f"saved to {path}",
                "authenticated": state.venues["kalshi"].authenticated}
    raise HTTPException(400, "set POLYMARKET_PRIVATE_KEY in the environment for Polymarket")


# --------------------------------------------------------------------------------------
# ESPN league
# --------------------------------------------------------------------------------------
@app.get("/api/espn/status")
def espn_status():
    """Whether an ESPN league is configured, and whether it actually connects."""
    creds = EspnCredentials.load()
    body = {
        "library_installed": espn_available(),
        "configured": creds is not None,
        "league_id": creds.league_id if creds else None,
        "private": creds.is_private if creds else False,
        # Without these the team picker has nothing to pre-select, and asks you
        # to choose your own team again on every visit.
        "team_id": creds.team_id if creds else None,
        "team_name": None,
        "roster_synced": len(state.espn_roster),
        "connected": False,
        "detail": "",
    }
    if not creds:
        body["detail"] = ("No league configured. Add your league id in Settings — "
                          "and your espn_s2 / SWID cookies if the league is private.")
        return _clean(body)
    try:
        body.update(state.espn().connect())
        body["team_id"] = creds.team_id
        if creds.team_id is not None:
            mine = state.espn().my_team()
            body["team_name"] = (mine or {}).get("team_name")
    except Exception as exc:  # noqa: BLE001
        body["detail"] = str(exc)
    return _clean(body)


@app.post("/api/espn/credentials")
def espn_credentials(req: EspnCredentialsRequest):
    """Save league id and cookies locally (file mode 0600), then verify."""
    # Re-saving cookies must not forget which team is yours: without carrying
    # the existing team_id forward, refreshing an expired espn_s2 quietly reset
    # the whole ESPN view to "which team is yours?".
    existing = EspnCredentials.load()
    keep_team = req.team_id if req.team_id is not None else (
        existing.team_id if existing and existing.league_id == req.league_id else None)
    creds = EspnCredentials(league_id=req.league_id, espn_s2=req.espn_s2.strip(),
                            swid=req.swid.strip(), year=req.year, team_id=keep_team)
    path = creds.save()
    state._espn = None
    try:
        info = state.espn(refresh=True).connect()
        return _clean({"ok": True, "saved_to": str(path), **info})
    except Exception as exc:  # noqa: BLE001
        return _clean({"ok": False, "saved_to": str(path), "detail": str(exc)})


@app.post("/api/espn/team")
def espn_set_team(req: EspnTeamRequest):
    """Remember which team in the league is yours.

    Saved on its own rather than only as a side effect of a full sync, so
    choosing your team once is enough — every later call falls back to this.
    """
    creds = EspnCredentials.load()
    if creds is None:
        raise HTTPException(400, "no ESPN league is configured yet")
    creds.team_id = int(req.team_id)
    creds.save()

    lg = state.espn(refresh=True)
    team = None
    try:
        team = lg.my_team()
    except Exception as exc:  # noqa: BLE001
        log.info("could not resolve the team name: %s", exc)

    # Refresh the saved roster too, so My League reflects the new choice at once.
    matched, unmatched = [], []
    try:
        matched, unmatched = _match_to_board(lg.my_roster(team_id=creds.team_id))
    except Exception as exc:  # noqa: BLE001
        log.info("could not refresh the roster for the new team: %s", exc)
    state.remember_roster(matched, team)

    return _clean({"saved": True, "team_id": creds.team_id, "team": team,
                   "matched": len(matched), "unmatched": unmatched})


@app.get("/api/espn/league")
def espn_league():
    """Teams, standings and every rostered player in your league."""
    try:
        lg = state.espn()
        return _clean({
            "info": lg.connect(),
            "teams": _df(lg.teams()),
            "rosters": _df(lg.rosters()),
        })
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, str(exc))


@app.post("/api/espn/compare")
def espn_compare(req: EspnCompareRequest):
    """Season-long: this model's projections against ESPN's, player by player."""
    a = arts()
    try:
        lg = state.espn()
        espn_players = lg.rosters()
        free = lg.free_agents(size=150)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, str(exc))

    if not free.is_empty():
        espn_players = pl.concat([espn_players, free], how="diagonal_relaxed")
    comparison = season_comparison(a.board, espn_players, min_points=req.min_points)
    if comparison.is_empty():
        raise HTTPException(422, "No players matched between the two boards. "
                                 "Check the league year matches the projection season.")
    return _clean({
        "n_compared": comparison.height,
        "agreement": agreement_stats(comparison),
        **disagreements(comparison, n=req.top_n),
        "all": _df(comparison, 400),
    })


@app.post("/api/espn/roster")
def espn_roster(team_id: int | None = None, team_name: str | None = None):
    """How the two boards value the players you actually own."""
    a = arts()
    try:
        lg = state.espn()
        mine = lg.my_roster(team_name=team_name, team_id=team_id)
        comparison = season_comparison(a.board, lg.rosters(), min_points=0.0)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, str(exc))
    return _clean(roster_report(mine, comparison))


@app.post("/api/espn/scorecard")
def espn_scorecard(req: EspnScoreRequest):
    """Score both projections against what actually happened, week by week.

    This is the only comparison that settles anything: ESPN's weekly projection
    and ours are both measured against the real result on the same players.
    """
    a = arts(need_sims=True)
    try:
        lg = state.espn()
        info = lg.connect()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, str(exc))

    current = int(info.get("current_week") or 1)
    weeks = req.weeks or list(range(1, max(current, 1)))
    weeks = [w for w in weeks if 1 <= w <= 18]
    if not weeks:
        return _clean({"scored": 0, "note": "no completed weeks yet this season",
                       "weeks_requested": []})

    frames = []
    for w in weeks:
        espn_week = lg.week_projections(w)
        if espn_week.is_empty():
            continue
        _, _, players = _games(w, req.n_sims)
        merged = weekly_comparison(players, espn_week, w)
        if not merged.is_empty():
            frames.append(merged)

    if not frames:
        return _clean({"scored": 0, "note": "no overlapping player-weeks found",
                       "weeks_requested": weeks})

    combined = pl.concat(frames, how="diagonal_relaxed")
    card = accuracy_scorecard(combined)
    worst = combined.filter(pl.col("espn_week_actual") > 0) if "espn_week_actual" in combined.columns else combined
    return _clean({
        **card,
        "weeks_requested": weeks,
        "biggest_misses": _df(worst.sort("gridiron_err", descending=True), 20)
                          if "gridiron_err" in worst.columns else [],
        "best_calls": _df(worst.filter(pl.col("closer") == "gridiron")
                          .sort("espn_err", descending=True), 20)
                      if "closer" in worst.columns else [],
    })


@app.get("/api/espn/week/{week}")
def espn_week(week: int, n_sims: int = Query(4000, ge=500, le=50_000)):
    """One week, both projections side by side (plus actuals once played)."""
    a = arts(need_sims=True)
    try:
        espn_week_df = state.espn().week_projections(week)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, str(exc))
    if espn_week_df.is_empty():
        raise HTTPException(404, f"ESPN returned no box scores for week {week}")
    _, _, players = _games(week, n_sims)
    merged = weekly_comparison(players, espn_week_df, week)
    return _clean({"week": week, "n": merged.height,
                   "scorecard": accuracy_scorecard(merged),
                   "players": _df(merged, 400)})


@app.post("/api/espn/sync")
def espn_sync(req: EspnSyncRequest):
    """Import everything from your ESPN league in one call.

    Connecting once should be enough: this pulls the league's real settings,
    your roster, the completed draft and your weekly schedule, so nothing has
    to be entered a second time by hand.
    """
    try:
        lg = state.espn()
        info = lg.connect()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, str(exc))

    if req.team_id is not None:
        lg.credentials.team_id = int(req.team_id)
        lg.credentials.save()

    result: dict = {"league": info, "imported": {}, "warnings": []}

    # ---- league settings -------------------------------------------------
    if req.settings:
        try:
            imported = lg.import_settings()
            if imported:
                current = pipeline.load_league().to_config()
                current["name"] = imported.get("name") or current.get("name")
                current["teams"] = imported.get("teams") or current["teams"]
                current["scoring_preset"] = imported.get("scoring_preset",
                                                         current["scoring_preset"])
                if imported.get("roster"):
                    current["roster"] = imported["roster"]
                if imported.get("scoring_overrides"):
                    current.setdefault("scoring", {}).update(imported["scoring_overrides"])
                league = LeagueSettings.from_dict(current)
                pipeline.save_league(league)
                pipeline.invalidate()
                state.draft = DraftState(league=league, my_slot=league.draft_slot)
                result["imported"]["settings"] = {
                    "teams": league.teams, "scoring_preset": league.scoring_preset,
                    "roster": league.roster,
                    "note": "rebuild simulations to apply these",
                }
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"settings: {exc}")

    # ---- your roster -----------------------------------------------------
    if req.roster:
        try:
            mine = lg.my_roster(team_id=req.team_id)
            matched, unmatched = _match_to_board(mine)
            state.remember_roster(matched, lg.my_team() or None)
            result["imported"]["roster"] = {
                "espn_players": mine.height, "matched": len(matched),
                "unmatched": unmatched,
                "team": lg.my_team() or None,
            }
            if unmatched:
                result["warnings"].append(
                    f"{len(unmatched)} ESPN players had no match on the projection board")
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"roster: {exc}")

    # ---- draft picks -----------------------------------------------------
    if req.draft:
        try:
            picks = lg.draft()
            if picks.is_empty():
                result["imported"]["draft"] = {"picks": 0, "note": "league has not drafted yet"}
            else:
                recorded = _apply_draft(picks)
                result["imported"]["draft"] = recorded
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"draft: {exc}")

    # ---- schedule --------------------------------------------------------
    if req.schedule:
        try:
            sched = lg.schedule()
            result["imported"]["schedule"] = _df(sched)
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"schedule: {exc}")

    state.espn_sync = result
    if req.rebuild:
        pipeline.start_background_build()
        result["rebuilding"] = True
    return _clean(result)


def _match_to_board(espn_players: pl.DataFrame) -> tuple[list[str], list[str]]:
    """Map ESPN players onto our player_ids by normalised name."""
    a = pipeline.get_artifacts(build_if_missing=False)
    if not a.ready or espn_players.is_empty():
        return [], []
    board = a.board
    if "merge_name" not in board.columns:
        from ..data.market import normalize_name
        board = board.with_columns(
            pl.col("player_name").map_elements(normalize_name, return_dtype=pl.Utf8)
            .alias("merge_name"))
    lookup = dict(zip(board["merge_name"].to_list(), board["player_id"].to_list()))
    matched, unmatched = [], []
    for row in espn_players.iter_rows(named=True):
        pid = lookup.get(row.get("merge_name"))
        (matched.append(pid) if pid else unmatched.append(row.get("player_name", "?")))
    return matched, unmatched


def _apply_draft(picks: pl.DataFrame) -> dict:
    """Replay a completed ESPN draft into the draft board."""
    a = pipeline.get_artifacts(build_if_missing=False)
    if not a.ready:
        return {"picks": int(picks.height), "recorded": 0,
                "note": "projections not built yet, so picks could not be matched"}
    board = a.board
    if "merge_name" not in board.columns:
        from ..data.market import normalize_name
        board = board.with_columns(
            pl.col("player_name").map_elements(normalize_name, return_dtype=pl.Utf8)
            .alias("merge_name"))
    lookup = dict(zip(board["merge_name"].to_list(), board["player_id"].to_list()))

    league = pipeline.load_league()
    state.draft = DraftState(league=league, my_slot=state.draft.my_slot)
    ordered = picks.sort(["round", "pick"])
    recorded, missing = 0, []
    for row in ordered.iter_rows(named=True):
        pid = lookup.get(row.get("merge_name"))
        if pid and pid not in state.draft.drafted:
            state.draft.record(pid)
            recorded += 1
        elif not pid:
            missing.append(row.get("player_name", "?"))
    return {"picks": int(picks.height), "recorded": recorded,
            "unmatched": missing[:20],
            "note": "draft board now reflects your real draft"}


@app.get("/api/espn/sync")
def espn_sync_status():
    """What the last sync imported."""
    return _clean({"last_sync": state.espn_sync,
                   "roster_size": len(state.espn_roster)})


@app.get("/api/espn/draft")
def espn_draft():
    try:
        return _clean({"picks": _df(state.espn().draft())})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, str(exc))


# --------------------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------------------
@app.get("/api/scheduler")
def scheduler_status():
    return _clean(state.scheduler.status())


@app.post("/api/scheduler/start")
def scheduler_start():
    state.scheduler.start()
    return _clean(state.scheduler.status())


@app.post("/api/scheduler/stop")
def scheduler_stop():
    state.scheduler.stop()
    return _clean(state.scheduler.status())


@app.post("/api/scheduler/run/{job}")
def scheduler_run(job: str):
    run = state.scheduler.run_job(job)
    if job == "rebuild_projections":
        state.game_cache.clear()
    return _clean(run.as_dict())


# --------------------------------------------------------------------------------------
# Live stream
# --------------------------------------------------------------------------------------
@app.websocket("/ws/markets")
async def ws_markets(ws: WebSocket):
    """Push quote snapshots and microstructure features to the sentiment panel."""
    await ws.accept()
    try:
        while True:
            payload = {
                "ts": time.time(),
                "polling": state.router.polling,
                "markets": [],
            }
            for m in state.watchlist[:40]:
                book = state.router._books.get(m.market_id)
                series = state.store.price_series(m.market_id, minutes=60)
                mids = np.array([r["mid"] for r in series if r["mid"] is not None], dtype=float)
                payload["markets"].append(_clean({
                    "venue": m.venue,
                    "market_id": m.market_id,
                    "title": m.title,
                    "mid": book.mid if book else m.mid,
                    "micro_price": book.micro_price() if book else None,
                    "spread": book.spread if book else m.spread,
                    "imbalance": book.imbalance() if book else 0.0,
                    "momentum": price_momentum(mids) if len(mids) > 3 else 0.0,
                    "realised_vol": realised_volatility(mids) if len(mids) > 3 else 0.0,
                    "history": [{"ts": r["ts"], "mid": r["mid"]} for r in series[-120:]],
                }))
            await ws.send_json(payload)
            await asyncio.sleep(2.0)
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        log.info("market websocket closed: %s", exc)


# --------------------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------------------
if WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIR / "assets")), name="assets")

    @app.get("/")
    def index():
        return FileResponse(str(WEB_DIR / "index.html"))

    # Both of these have to answer from the root: a service worker may only
    # control the paths below its own URL, and a worker served from /assets
    # could not intercept the app shell at /.
    @app.get("/sw.js")
    def service_worker():
        return FileResponse(str(WEB_DIR / "sw.js"), media_type="text/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(str(WEB_DIR / "manifest.webmanifest"),
                            media_type="application/manifest+json")

    @app.get("/apple-touch-icon.png")
    @app.get("/apple-touch-icon-precomposed.png")
    def apple_icon():
        """iOS asks for this at the root before it reads any <link> tag."""
        return FileResponse(str(WEB_DIR / "assets" / "apple-touch-icon.png"),
                            media_type="image/png")


@app.exception_handler(Exception)
async def unhandled(request, exc):  # noqa: ANN001
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.on_event("startup")
def _on_startup():
    """Warm the cache from disk and start the first simulation immediately."""
    try:
        pipeline.hydrate_from_disk()
        pipeline.start_background_build()
    except Exception as exc:  # noqa: BLE001
        log.warning("startup build could not be started: %s", exc)


def run(host: str = "127.0.0.1", port: int = 8000, reload: bool = False,
        start_scheduler: bool = True, require_token: bool | None = None):
    import uvicorn

    # Anything other than a loopback bind puts the panel — and the endpoints
    # that can place orders — on the network, so the gate goes up by default.
    if require_token is None:
        require_token = not net.is_loopback_host(host)
    access.configure(required=require_token, host=host, port=port)

    if start_scheduler:
        state.scheduler.start()
    uvicorn.run("gridiron.api.server:app" if reload else app, host=host, port=port,
                reload=reload, log_level="info")
