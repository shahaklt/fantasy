"""The refresh pipeline: pull data, rebuild projections, simulate, cache results.

Everything the app serves is produced here and written to ``data/artifacts`` so
the UI never waits on a simulation. A rebuild is idempotent -- run it as often
as you like -- and the scheduler runs it twice a day.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from .config import ARTIFACT_DIR, DEFAULT_SIMS, current_season, detect_backend
from .data import market as market_data
from .data import nflverse as nv
from .draft import add_value_columns, assign_tiers, board_with_availability, snake_picks
from .models.projections import ProjectionSet, build_projections
from .scoring import LeagueSettings
from .sim import MonteCarloEngine, build_sim_inputs

log = logging.getLogger(__name__)

PROJECTIONS_PATH = ARTIFACT_DIR / "projections.parquet"
BOARD_PATH = ARTIFACT_DIR / "draft_board.parquet"
TEAM_WEEKS_PATH = ARTIFACT_DIR / "team_weeks.parquet"
WEEKLY_PATH = ARTIFACT_DIR / "weekly_points.npy"
META_PATH = ARTIFACT_DIR / "meta.json"
LEAGUE_PATH = ARTIFACT_DIR / "league.json"


@dataclass
class Artifacts:
    """Everything a request might need, held in memory once built."""

    league: LeagueSettings
    season: int
    projections: ProjectionSet | None = None
    board: pl.DataFrame | None = None
    season_result: object | None = None
    engine: MonteCarloEngine | None = None
    meta: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.board is not None and self.projections is not None


_CACHE: Artifacts | None = None


def load_league() -> LeagueSettings:
    if LEAGUE_PATH.exists():
        try:
            return LeagueSettings.from_dict(json.loads(LEAGUE_PATH.read_text()))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read saved league settings: %s", exc)
    return LeagueSettings()


def save_league(league: LeagueSettings) -> None:
    LEAGUE_PATH.write_text(json.dumps(league.to_config(), indent=2))


def refresh_data(force: bool = False) -> dict:
    """Pull fresh source data. Safe to call on a schedule."""
    season = current_season()
    started = time.time()
    results: dict[str, str] = {}

    steps = [
        ("schedules", lambda: nv.load_schedules([season - 1, season], force=force)),
        ("rosters", lambda: nv.load_rosters([season], force=force)),
        ("depth_charts", lambda: nv.load_depth_charts([season], force=force)),
        ("player_stats", lambda: nv.load_player_stats(
            list(range(season - 3, season + 1)), force=force)),
        ("team_stats", lambda: nv.load_team_stats(
            list(range(season - 3, season + 1)), force=force)),
        ("snap_counts", lambda: nv.load_snap_counts(
            list(range(season - 3, season + 1)), force=force)),
        ("injuries", lambda: nv.load_injuries([season], force=force)),
        ("market_ecr", lambda: market_data.load_ecr(force=force)),
        ("player_ids", lambda: market_data.load_playerids(force=force)),
    ]
    for name, fn in steps:
        try:
            df = fn()
            results[name] = f"ok ({getattr(df, 'height', 0)} rows)"
        except Exception as exc:  # noqa: BLE001
            results[name] = f"failed: {exc}"
            log.warning("refresh step %s failed: %s", name, exc)

    return {"steps": results, "seconds": round(time.time() - started, 1),
            "season": season, "at": time.strftime("%Y-%m-%d %H:%M:%S")}


def build_all(league: LeagueSettings | None = None, season: int | None = None,
              n_sims: int = DEFAULT_SIMS, keep_weekly: bool = True,
              seed: int | None = 2026) -> Artifacts:
    """Rebuild projections, run the season simulation and assemble the draft board."""
    global _CACHE

    league = league or load_league()
    season = season or current_season()
    started = time.time()

    proj = build_projections(season, league)
    inputs = build_sim_inputs(proj)
    engine = MonteCarloEngine(inputs, league.scoring, seed=seed)
    result = engine.simulate_season(n_sims=n_sims, keep_weekly=keep_weekly)

    board = result.percentiles()
    extra_cols = [c for c in ("adp", "adp_sd", "market_rank", "bye", "age", "depth_rank",
                              "is_rookie", "role_confidence", "target_share_proj",
                              "carry_share_proj", "dropback_share_proj")
                  if c in proj.players.columns]
    board = board.join(proj.players.select(["player_id"] + extra_cols), on="player_id", how="left")
    board = add_value_columns(board, league)
    board = assign_tiers(board)
    picks = snake_picks(league.draft_slot, league.teams, league.roster_size)
    board = board_with_availability(board, picks)

    meta = {
        "season": season,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seconds": round(time.time() - started, 1),
        "n_sims": n_sims,
        "players": board.height,
        "backend": detect_backend().describe(),
        "market_sources": proj.market_sources,
        "projection_summary": proj.summary(),
    }

    try:
        board.write_parquet(BOARD_PATH)
        proj.players.write_parquet(PROJECTIONS_PATH)
        proj.team_weeks.write_parquet(TEAM_WEEKS_PATH)
        META_PATH.write_text(json.dumps(meta, indent=2, default=str))
        save_league(league)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not persist artifacts: %s", exc)

    _CACHE = Artifacts(league=league, season=season, projections=proj, board=board,
                       season_result=result, engine=engine, meta=meta)
    log.info("built artifacts in %.1fs (%d players, %d sims)", meta["seconds"], board.height, n_sims)
    return _CACHE


# A rebuild is CPU-heavy and holds the interpreter for tens of seconds, so it
# always runs on a worker thread; requests never trigger one inline.
_BUILD_LOCK = threading.Lock()
_BUILD_STATE: dict = {"building": False, "started": None, "error": None, "stage": "idle"}


def build_state() -> dict:
    return dict(_BUILD_STATE)


def hydrate_from_disk() -> Artifacts | None:
    """Load the last saved board without simulating.

    Lets the draft board and projections be usable the instant the server
    starts, while the full simulation (which the game and lineup views need)
    rebuilds in the background.
    """
    global _CACHE
    if not (BOARD_PATH.exists() and PROJECTIONS_PATH.exists()):
        return None
    try:
        board = pl.read_parquet(BOARD_PATH)
        players = pl.read_parquet(PROJECTIONS_PATH)
        team_weeks = pl.read_parquet(TEAM_WEEKS_PATH) if TEAM_WEEKS_PATH.exists() else pl.DataFrame()
        meta = json.loads(META_PATH.read_text()) if META_PATH.exists() else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("could not hydrate artifacts from disk: %s", exc)
        return None

    league = load_league()
    proj = ProjectionSet(players=players, team_weeks=team_weeks,
                         season=int(meta.get("season") or current_season()),
                         league=league, context=None,
                         market_sources=meta.get("market_sources", []))
    meta = {**meta, "from_disk": True}
    _CACHE = Artifacts(league=league, season=proj.season, projections=proj, board=board,
                       season_result=None, engine=None, meta=meta)
    log.info("hydrated %d players from the last saved build", board.height)
    return _CACHE


def start_background_build(**kwargs) -> bool:
    """Kick off a rebuild on a worker thread. Returns False if one is running."""
    if not _BUILD_LOCK.acquire(blocking=False):
        return False

    def worker():
        _BUILD_STATE.update(building=True, started=time.strftime("%H:%M:%S"),
                            error=None, stage="simulating")
        try:
            build_all(**kwargs)
            _BUILD_STATE.update(stage="ready", error=None)
        except Exception as exc:  # noqa: BLE001
            log.exception("background build failed")
            _BUILD_STATE.update(stage="failed", error=str(exc))
        finally:
            _BUILD_STATE.update(building=False)
            _BUILD_LOCK.release()

    threading.Thread(target=worker, name="gridiron-build", daemon=True).start()
    return True


def get_artifacts(build_if_missing: bool = True, **kwargs) -> Artifacts:
    """In-memory artifacts; hydrates from disk and rebuilds in the background."""
    global _CACHE
    if _CACHE is not None and _CACHE.ready:
        return _CACHE
    hydrated = hydrate_from_disk()
    if build_if_missing and (hydrated is None or hydrated.season_result is None):
        start_background_build(**kwargs)
    if hydrated is not None:
        return hydrated
    return Artifacts(league=load_league(), season=current_season())


def load_cached_board() -> pl.DataFrame | None:
    """Read the last saved board without running a simulation."""
    if BOARD_PATH.exists():
        try:
            return pl.read_parquet(BOARD_PATH)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read cached board: %s", exc)
    return None


def invalidate() -> None:
    global _CACHE
    _CACHE = None
