"""Tiny parquet-backed cache so the whole app works offline after one warm-up."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

import polars as pl

from ..config import CACHE_DIR

log = logging.getLogger(__name__)

#: How long cached artefacts stay fresh, in seconds.
DEFAULT_TTL = 12 * 3600
LONG_TTL = 30 * 24 * 3600  # completed historical seasons never change


def cache_path(key: str) -> Path:
    p = CACHE_DIR / f"{key}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def is_fresh(path: Path, ttl: float) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < ttl


def cached_frame(
    key: str,
    loader: Callable[[], pl.DataFrame],
    ttl: float = DEFAULT_TTL,
    force: bool = False,
) -> pl.DataFrame:
    """Return ``loader()`` result, memoised on disk as parquet.

    If the network call fails but a stale copy exists, the stale copy is used --
    a draft room should never go dark because GitHub had a bad minute.
    """
    path = cache_path(key)
    if not force and is_fresh(path, ttl):
        try:
            return pl.read_parquet(path)
        except Exception:  # corrupt cache -> refetch
            log.warning("cache read failed for %s, refetching", key)

    try:
        df = loader()
    except Exception as exc:  # noqa: BLE001
        if path.exists():
            log.warning("fetch failed for %s (%s); serving stale cache", key, exc)
            return pl.read_parquet(path)
        raise
    if df is None:
        raise RuntimeError(f"loader for {key} returned None")
    try:
        df.write_parquet(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not persist cache for %s: %s", key, exc)
    return df


def clear_cache(prefix: str | None = None) -> int:
    n = 0
    for p in CACHE_DIR.glob("**/*.parquet"):
        if prefix is None or p.name.startswith(prefix):
            p.unlink()
            n += 1
    return n
