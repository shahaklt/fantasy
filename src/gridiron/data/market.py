"""Market data: consensus rankings / ADP, plus optional league-platform adapters.

Primary source is the DynastyProcess mirror of FantasyPros expert consensus
rankings (ECR) -- it ships ``ecr`` (mean rank), ``sd``, ``best`` and ``worst``,
which is exactly the shape needed to model *where a player actually goes* in a
draft rather than just where he is ranked.

Optional live sources (Sleeper, FantasyFootballCalculator) are used when
reachable; a user-supplied CSV always wins so you can paste in your own board.
"""
from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass

import polars as pl
import requests

from ..config import USER_DIR
from .cache import DEFAULT_TTL, cached_frame

log = logging.getLogger(__name__)

ECR_URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_fpecr_latest.csv"
PLAYER_IDS_URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv"
FFC_URL = "https://fantasyfootballcalculator.com/api/v1/adp/{fmt}"
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_TEAM_FIX = {"OAK": "LV", "SD": "LAC", "STL": "LA", "LAR": "LA", "WSH": "WAS", "JAC": "JAX"}


def normalize_name(name: str | None) -> str:
    """`A.J. Brown` / `AJ Brown Jr.` -> `ajbrown`; the join key for merging sources."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    s = s.lower().replace("'", "").replace(".", " ").replace("-", " ")
    s = re.sub(r"[^a-z\s]", " ", s)
    parts = [p for p in s.split() if p and p not in _SUFFIXES]
    return "".join(parts)


def _norm_expr(col: str) -> pl.Expr:
    return (
        pl.col(col)
        .cast(pl.Utf8)
        .fill_null("")
        .map_elements(normalize_name, return_dtype=pl.Utf8)
        .alias("merge_name")
    )


# --------------------------------------------------------------------------------------
# FantasyPros ECR (via DynastyProcess mirror)
# --------------------------------------------------------------------------------------
def _fetch_csv(url: str, timeout: int = 60) -> pl.DataFrame:
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "gridiron/1.0"})
    resp.raise_for_status()
    return pl.read_csv(io.BytesIO(resp.content), infer_schema_length=20000, ignore_errors=True)


def load_ecr(force: bool = False) -> pl.DataFrame:
    """Full FantasyPros ECR table (all page types: redraft / dynasty / best-ball)."""
    return cached_frame("market_ecr", lambda: _fetch_csv(ECR_URL), ttl=6 * 3600, force=force)


def load_playerids(force: bool = False) -> pl.DataFrame:
    """Cross-site id map (gsis / sleeper / espn / yahoo / fantasypros / pfr)."""
    return cached_frame("market_playerids", lambda: _fetch_csv(PLAYER_IDS_URL), ttl=DEFAULT_TTL, force=force)


def consensus_board(page_type: str = "redraft-overall", force: bool = False) -> pl.DataFrame:
    """Consensus draft board: one row per player with mean rank and dispersion.

    Returns columns: ``merge_name, player, pos, team, ecr, ecr_sd, best, worst, bye``.
    """
    raw = load_ecr(force=force)
    if raw.is_empty():
        return pl.DataFrame()
    df = raw.filter(pl.col("page_type") == page_type)
    if df.is_empty():  # fall back to any redraft board present
        df = raw.filter(pl.col("page_type").str.starts_with("redraft"))
    if df.is_empty():
        return pl.DataFrame()

    df = df.with_columns(
        pl.col("ecr").cast(pl.Float64, strict=False),
        pl.col("sd").cast(pl.Float64, strict=False).alias("ecr_sd"),
        pl.col("best").cast(pl.Float64, strict=False),
        pl.col("worst").cast(pl.Float64, strict=False),
        pl.col("bye").cast(pl.Float64, strict=False),
        pl.col("team").cast(pl.Utf8).replace(_TEAM_FIX),
        pl.col("pos").cast(pl.Utf8).str.to_uppercase(),
        _norm_expr("player"),
    )
    df = df.filter(pl.col("ecr").is_not_null() & (pl.col("merge_name") != ""))
    keep = ["merge_name", "player", "pos", "team", "ecr", "ecr_sd", "best", "worst", "bye", "scrape_date"]
    keep = [c for c in keep if c in df.columns]
    df = df.select(keep).unique(subset=["merge_name"], keep="first").sort("ecr")
    # A missing sd (rare, deep bench players) gets a wide default so the
    # availability model treats them as genuinely unpredictable.
    return df.with_columns(
        pl.col("ecr_sd").fill_null(pl.col("ecr") * 0.25 + 5.0).clip(1.0, 80.0)
    )


# --------------------------------------------------------------------------------------
# Optional live ADP sources
# --------------------------------------------------------------------------------------
def load_ffc_adp(fmt: str = "half-ppr", teams: int = 12, year: int | None = None,
                 force: bool = False) -> pl.DataFrame:
    """FantasyFootballCalculator real-mock-draft ADP. Returns empty frame if blocked."""
    from ..config import current_season

    year = year or current_season()
    key = f"market_ffc_{fmt}_{teams}_{year}"

    def _load():
        url = FFC_URL.format(fmt=fmt)
        r = requests.get(url, params={"teams": teams, "year": year}, timeout=30,
                         headers={"User-Agent": "gridiron/1.0"})
        r.raise_for_status()
        players = r.json().get("players", [])
        if not players:
            raise RuntimeError("FFC returned no players")
        return pl.DataFrame(players, infer_schema_length=None)

    try:
        df = cached_frame(key, _load, ttl=6 * 3600, force=force)
    except Exception as exc:  # noqa: BLE001
        log.info("FFC ADP unavailable (%s)", exc)
        return pl.DataFrame()

    ren = {"adp": "adp", "adp_formatted": "adp_formatted", "stdev": "adp_sd",
           "position": "pos", "name": "player"}
    df = df.rename({k: v for k, v in ren.items() if k in df.columns})
    if "player" not in df.columns:
        return pl.DataFrame()
    df = df.with_columns(_norm_expr("player"),
                         pl.col("adp").cast(pl.Float64, strict=False),
                         pl.col("adp_sd").cast(pl.Float64, strict=False))
    keep = [c for c in ["merge_name", "player", "pos", "team", "adp", "adp_sd", "times_drafted"] if c in df.columns]
    return df.select(keep).unique(subset=["merge_name"], keep="first")


def load_sleeper_players(force: bool = False) -> pl.DataFrame:
    """Sleeper master player list (ids, injury status, depth chart order)."""
    def _load():
        r = requests.get(SLEEPER_PLAYERS_URL, timeout=90, headers={"User-Agent": "gridiron/1.0"})
        r.raise_for_status()
        data = r.json()
        rows = []
        for pid, p in data.items():
            if not isinstance(p, dict):
                continue
            rows.append({
                "sleeper_id": pid,
                "player": p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip(),
                "pos": p.get("position"),
                "team": p.get("team"),
                "status": p.get("status"),
                "injury_status": p.get("injury_status"),
                "depth_chart_order": p.get("depth_chart_order"),
                "years_exp": p.get("years_exp"),
                "gsis_id": p.get("gsis_id"),
                "search_rank": p.get("search_rank"),
            })
        return pl.DataFrame(rows, infer_schema_length=None)

    try:
        df = cached_frame("market_sleeper_players", _load, ttl=24 * 3600, force=force)
    except Exception as exc:  # noqa: BLE001
        log.info("Sleeper player list unavailable (%s)", exc)
        return pl.DataFrame()
    if df.is_empty():
        return df
    return df.with_columns(_norm_expr("player"))


# --------------------------------------------------------------------------------------
# User overrides
# --------------------------------------------------------------------------------------
USER_ADP_PATH = USER_DIR / "adp.csv"
USER_PROJECTIONS_PATH = USER_DIR / "projections.csv"


def load_user_adp() -> pl.DataFrame:
    """Optional ``data/user/adp.csv`` -- columns: player, pos, adp[, adp_sd, team]."""
    if not USER_ADP_PATH.exists():
        return pl.DataFrame()
    try:
        df = pl.read_csv(USER_ADP_PATH, infer_schema_length=10000, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read %s: %s", USER_ADP_PATH, exc)
        return pl.DataFrame()
    cols = {c.lower().strip(): c for c in df.columns}
    name_col = next((cols[c] for c in ("player", "name", "player_name", "full_name") if c in cols), None)
    adp_col = next((cols[c] for c in ("adp", "rank", "ecr", "overall") if c in cols), None)
    if not name_col or not adp_col:
        log.warning("%s needs a player-name column and an adp/rank column", USER_ADP_PATH)
        return pl.DataFrame()
    out = df.rename({name_col: "player", adp_col: "adp"})
    if "sd" in cols:
        out = out.rename({cols["sd"]: "adp_sd"})
    elif "adp_sd" in cols:
        out = out.rename({cols["adp_sd"]: "adp_sd"})
    out = out.with_columns(_norm_expr("player"), pl.col("adp").cast(pl.Float64, strict=False))
    if "adp_sd" in out.columns:
        out = out.with_columns(pl.col("adp_sd").cast(pl.Float64, strict=False))
    keep = [c for c in ["merge_name", "player", "pos", "team", "adp", "adp_sd"] if c in out.columns]
    return out.select(keep).drop_nulls("adp").unique(subset=["merge_name"], keep="first")


@dataclass
class MarketBoard:
    """Merged market view with a documented provenance for each row."""

    frame: pl.DataFrame
    sources: list[str]

    def __len__(self) -> int:
        return self.frame.height


def build_market_board(scoring_preset: str = "half_ppr", teams: int = 12,
                       force: bool = False) -> MarketBoard:
    """Merge every available ADP/ECR source into a single draft-position prior.

    Priority: user CSV > FantasyFootballCalculator mock ADP > FantasyPros ECR.
    Whichever sources exist are blended (ranks averaged) and the dispersion of
    the winning source is kept for the availability model.
    """
    page = {"ppr": "redraft-overall", "half_ppr": "redraft-overall",
            "standard": "redraft-overall"}.get(scoring_preset, "redraft-overall")
    sources: list[str] = []

    ecr = consensus_board(page, force=force)
    if not ecr.is_empty():
        sources.append("fantasypros_ecr")
        board = ecr.rename({"ecr": "adp", "ecr_sd": "adp_sd"})
    else:
        board = pl.DataFrame()

    fmt = {"ppr": "ppr", "half_ppr": "half-ppr", "standard": "standard"}.get(scoring_preset, "half-ppr")
    ffc = load_ffc_adp(fmt=fmt, teams=teams, force=force)
    if not ffc.is_empty():
        sources.append("ffcalculator")
        if board.is_empty():
            board = ffc
        else:
            board = board.join(ffc.select(["merge_name", "adp", "adp_sd"]), on="merge_name",
                               how="left", suffix="_ffc")
            board = board.with_columns(
                pl.when(pl.col("adp_ffc").is_not_null())
                .then((pl.col("adp") + pl.col("adp_ffc")) / 2.0)
                .otherwise(pl.col("adp")).alias("adp"),
                pl.when(pl.col("adp_sd_ffc").is_not_null())
                .then(pl.max_horizontal("adp_sd", "adp_sd_ffc"))
                .otherwise(pl.col("adp_sd")).alias("adp_sd"),
            ).drop([c for c in ("adp_ffc", "adp_sd_ffc") if c in board.columns])

    user = load_user_adp()
    if not user.is_empty():
        sources.append("user_csv")
        if board.is_empty():
            board = user
        else:
            board = board.join(user.select(["merge_name", "adp", "adp_sd"]).rename(
                {"adp": "adp_user", "adp_sd": "adp_sd_user"} if "adp_sd" in user.columns
                else {"adp": "adp_user"}), on="merge_name", how="full", coalesce=True)
            board = board.with_columns(
                pl.coalesce(["adp_user", "adp"]).alias("adp"),
                pl.coalesce([c for c in ("adp_sd_user", "adp_sd") if c in board.columns]).alias("adp_sd"),
            ).drop([c for c in ("adp_user", "adp_sd_user") if c in board.columns])

    if board.is_empty():
        return MarketBoard(pl.DataFrame(schema={"merge_name": pl.Utf8, "adp": pl.Float64}), sources)

    board = board.filter(pl.col("adp").is_not_null()).sort("adp")
    board = board.with_columns(
        pl.col("adp_sd").fill_null(pl.col("adp") * 0.25 + 5.0).clip(1.0, 90.0),
        pl.int_range(1, pl.len() + 1).cast(pl.Float64).alias("market_rank"),
    )
    return MarketBoard(board, sources)
