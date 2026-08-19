"""FastAPI application: the whole engine behind one local HTTP server.

Runs entirely on your machine. The only outbound calls are to the public data
sources (nflverse, FantasyPros mirror) and, if you configure them, the trading
venues. Nothing is uploaded anywhere.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import numpy as np
import polars as pl
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import pipeline
from ..analysis import GameAnalyst
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
from .schemas import (CredentialsRequest, DraftPick, DraftReset, LeagueSimRequest,
                      LeagueUpdate, LineupRequest, PollRequest, RebuildRequest,
                      RecommendRequest, SignalRequest, TradeRequest)

log = logging.getLogger(__name__)

WEB_DIR = REPO_ROOT / "web"

app = FastAPI(title="Gridiron", version="1.0.0",
              description="Monte Carlo fantasy football and prediction-market engine")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


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
@app.post("/api/lineup")
def lineup(req: LineupRequest):
    a = arts(need_sims=True)
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
        start_scheduler: bool = True):
    import uvicorn

    if start_scheduler:
        state.scheduler.start()
    uvicorn.run("gridiron.api.server:app" if reload else app, host=host, port=port,
                reload=reload, log_level="info")
