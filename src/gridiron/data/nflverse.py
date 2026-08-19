"""nflverse loaders (play-by-play, weekly stats, rosters, schedules, ...).

Everything funnels through :func:`gridiron.data.cache.cached_frame` so a warm
cache means zero network calls -- important when you are on the clock in a draft.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import polars as pl

from ..config import MIN_TRAIN_SEASON, current_season
from .cache import DEFAULT_TTL, LONG_TTL, cached_frame

log = logging.getLogger(__name__)


def _nflreadpy():
    try:
        import nflreadpy  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - dependency guaranteed by pyproject
        raise RuntimeError(
            "nflreadpy is required for data loading. Install with: pip install nflreadpy"
        ) from exc
    return nflreadpy


def _load_by_season(fn_name: str, seasons: list[int], **kwargs) -> pl.DataFrame:
    """Call an nflreadpy loader per-season, skipping seasons not yet published.

    The upcoming season has a schedule and rosters long before it has any stats,
    so a 404 on the newest year is expected, not an error.
    """
    fn = getattr(_nflreadpy(), fn_name)
    frames: list[pl.DataFrame] = []
    missing: list[int] = []
    for season in seasons:
        try:
            frames.append(fn([season], **kwargs))
        except Exception as exc:  # noqa: BLE001
            missing.append(season)
            log.debug("%s unavailable for %s (%s)", fn_name, season, exc)
    if missing:
        log.info("%s: no data yet for %s", fn_name, missing)
    if not frames:
        raise RuntimeError(f"{fn_name} returned no data for seasons {seasons}")
    if len(frames) == 1:
        return frames[0]
    return pl.concat(frames, how="diagonal_relaxed")


def _ttl_for(seasons: list[int]) -> float:
    """Finished seasons are immutable; the live one refreshes every 12h."""
    return LONG_TTL if max(seasons) < current_season() else DEFAULT_TTL


def _norm_seasons(seasons: int | list[int] | None, default_start: int = MIN_TRAIN_SEASON) -> list[int]:
    if seasons is None:
        return list(range(default_start, current_season() + 1))
    if isinstance(seasons, int):
        return [seasons]
    return sorted({int(s) for s in seasons})


# --------------------------------------------------------------------------------------
# Core tables
# --------------------------------------------------------------------------------------
def load_schedules(seasons: int | list[int] | None = None, force: bool = False) -> pl.DataFrame:
    """Game schedule + results + closing Vegas spread/total.

    The upcoming season's full slate (with market lines) is published well before
    kickoff, which is what drives the team-total component of the simulator.
    """
    seasons = _norm_seasons(seasons, 1999)
    key = f"schedules_{seasons[0]}_{seasons[-1]}"

    def _load():
        return _nflreadpy().load_schedules(seasons)

    return cached_frame(key, _load, ttl=DEFAULT_TTL, force=force)


def load_player_stats(seasons: int | list[int] | None = None, force: bool = False) -> pl.DataFrame:
    """Weekly player box score + advanced (target share, air yards, EPA)."""
    seasons = _norm_seasons(seasons)
    key = f"player_stats_{seasons[0]}_{seasons[-1]}"

    def _load():
        return _load_by_season("load_player_stats", seasons)

    return cached_frame(key, _load, ttl=_ttl_for(seasons), force=force)


def load_team_stats(seasons: int | list[int] | None = None, force: bool = False) -> pl.DataFrame:
    seasons = _norm_seasons(seasons)
    key = f"team_stats_{seasons[0]}_{seasons[-1]}"

    def _load():
        return _load_by_season("load_team_stats", seasons)

    return cached_frame(key, _load, ttl=_ttl_for(seasons), force=force)


def load_pbp(seasons: int | list[int], force: bool = False) -> pl.DataFrame:
    """Play-by-play. Large -- request only the seasons you need."""
    seasons = _norm_seasons(seasons)
    key = f"pbp_{seasons[0]}_{seasons[-1]}"

    def _load():
        return _load_by_season("load_pbp", seasons)

    return cached_frame(key, _load, ttl=_ttl_for(seasons), force=force)


def load_snap_counts(seasons: int | list[int] | None = None, force: bool = False) -> pl.DataFrame:
    seasons = _norm_seasons(seasons, max(MIN_TRAIN_SEASON, 2012))
    key = f"snap_counts_{seasons[0]}_{seasons[-1]}"

    def _load():
        return _load_by_season("load_snap_counts", seasons)

    return cached_frame(key, _load, ttl=_ttl_for(seasons), force=force)


def load_rosters(seasons: int | list[int] | None = None, force: bool = False) -> pl.DataFrame:
    seasons = _norm_seasons(seasons, current_season())
    key = f"rosters_{seasons[0]}_{seasons[-1]}"

    def _load():
        return _load_by_season("load_rosters", seasons)

    return cached_frame(key, _load, ttl=DEFAULT_TTL, force=force)


def load_players(force: bool = False) -> pl.DataFrame:
    """Master player table (ids, position, birthdate, draft capital)."""
    return cached_frame("players", lambda: _nflreadpy().load_players(), ttl=DEFAULT_TTL, force=force)


def load_depth_charts(seasons: int | list[int] | None = None, force: bool = False) -> pl.DataFrame:
    seasons = _norm_seasons(seasons, current_season())
    key = f"depth_charts_{seasons[0]}_{seasons[-1]}"

    def _load():
        return _load_by_season("load_depth_charts", seasons)

    return cached_frame(key, _load, ttl=DEFAULT_TTL, force=force)


def load_injuries(seasons: int | list[int] | None = None, force: bool = False) -> pl.DataFrame:
    seasons = _norm_seasons(seasons, current_season())
    key = f"injuries_{seasons[0]}_{seasons[-1]}"

    def _load():
        return _load_by_season("load_injuries", seasons)

    try:
        return cached_frame(key, _load, ttl=DEFAULT_TTL, force=force)
    except Exception as exc:  # pre-season: report not yet published
        log.info("injury report unavailable for %s (%s)", seasons, exc)
        return pl.DataFrame()


def load_teams(force: bool = False) -> pl.DataFrame:
    return cached_frame("teams", lambda: _nflreadpy().load_teams(), ttl=LONG_TTL, force=force)


# --------------------------------------------------------------------------------------
# Convenience views
# --------------------------------------------------------------------------------------
@lru_cache(maxsize=4)
def team_colors() -> dict[str, str]:
    try:
        t = load_teams()
        col = "team_color" if "team_color" in t.columns else None
        abbr = "team_abbr" if "team_abbr" in t.columns else "team"
        if col is None:
            return {}
        return {r[0]: r[1] for r in t.select([abbr, col]).drop_nulls().iter_rows()}
    except Exception:  # noqa: BLE001
        return {}


def current_rosters(season: int | None = None) -> pl.DataFrame:
    """Fantasy-relevant players on an NFL roster for `season`."""
    season = season or current_season()
    r = load_rosters([season])
    name_col = "full_name" if "full_name" in r.columns else "player_name"
    keep = ["gsis_id", name_col, "team", "position", "status", "years_exp", "birth_date"]
    keep = [c for c in keep if c in r.columns]
    out = r.select(keep).rename({name_col: "player_name", "gsis_id": "player_id"})
    return out.filter(pl.col("position").is_in(["QB", "RB", "WR", "TE", "K", "FB"]))


def latest_depth_chart(season: int | None = None) -> pl.DataFrame:
    """Most recent depth-chart snapshot per team, one row per player."""
    season = season or current_season()
    dc = load_depth_charts([season])
    if dc.is_empty():
        return dc
    if "dt" in dc.columns:
        dc = dc.with_columns(pl.col("dt").cast(pl.Utf8))
        latest = dc.select(pl.col("dt").max()).item()
        dc = dc.filter(pl.col("dt") == latest)
    rank_col = "pos_rank" if "pos_rank" in dc.columns else "depth_team"
    pos_col = "pos_abb" if "pos_abb" in dc.columns else "position"
    keep = [c for c in ["gsis_id", "player_name", "team", pos_col, rank_col, "pos_grp"] if c in dc.columns]
    out = dc.select(keep).rename({"gsis_id": "player_id", pos_col: "position", rank_col: "depth_rank"})
    out = out.with_columns(pl.col("depth_rank").cast(pl.Float64, strict=False))
    return out.unique(subset=["player_id", "position"], keep="first")


def team_abbr_fixes(df: pl.DataFrame, cols: list[str]) -> pl.DataFrame:
    """Normalise historical relocations/renames to current abbreviations."""
    mapping = {"OAK": "LV", "SD": "LAC", "STL": "LA", "LAR": "LA", "OTI": "TEN",
               "HTX": "HOU", "CLV": "CLE", "BLT": "BAL", "ARZ": "ARI", "AZ": "ARI",
               "SL": "LA", "JAC": "JAX", "WSH": "WAS", "WFT": "WAS", "KAN": "KC",
               "GNB": "GB", "NWE": "NE", "NOR": "NO", "SFO": "SF", "TAM": "TB",
               "RAM": "LA", "RAI": "LV", "SDG": "LAC"}
    exprs = []
    for c in cols:
        if c in df.columns:
            exprs.append(pl.col(c).replace(mapping).alias(c))
    return df.with_columns(exprs) if exprs else df
