"""ESPN fantasy league adapter, built on cwendt94/espn-api.

Pulls your actual league: teams, rosters, ESPN's own projections (season and
per week), free agents and matchups. That makes two things possible that the
rest of the app cannot do on its own — importing your real roster instead of
typing it in, and scoring this model against ESPN's on the same players.

Private leagues require two cookies from a logged-in browser session,
``espn_s2`` and ``SWID``. They are read from the environment or a local file
and never leave the machine.

Getting the cookies (Chrome or Firefox): sign in to fantasy.espn.com, open
DevTools → Application → Cookies → https://fantasy.espn.com, and copy the
values of ``espn_s2`` and ``SWID`` (keep the braces on SWID).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from ..config import USER_DIR, current_season
from .market import normalize_name

log = logging.getLogger(__name__)

CREDENTIALS_PATH = USER_DIR / "espn_credentials.json"
ROSTER_PATH = USER_DIR / "espn_roster.json"

#: ESPN slot names -> the position vocabulary the rest of the app uses.
POSITION_FIX = {"D/ST": "DST", "DST": "DST", "K": "K", "QB": "QB",
                "RB": "RB", "WR": "WR", "TE": "TE", "FB": "RB"}
#: ESPN pro-team abbreviations that differ from nflverse's.
TEAM_FIX = {"WSH": "WAS", "JAC": "JAX", "LAR": "LA", "ARZ": "ARI", "OAK": "LV", "SD": "LAC"}

#: ESPN lineup-slot names -> our slot vocabulary.
SLOT_FIX = {
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K", "D/ST": "DST",
    "RB/WR/TE": "FLEX", "RB/WR": "WRRB_FLEX", "WR/TE": "REC_FLEX",
    "OP": "SUPERFLEX", "TQB": "QB", "BE": "BN", "IR": "IR",
}
#: ESPN scoring stat ids we need to read the format off.
STAT_RECEPTION = 53
STAT_PASS_TD = 4
STAT_PASS_YDS = 3


@dataclass
class EspnCredentials:
    """League id plus the two cookies a private league needs."""

    league_id: int
    espn_s2: str = ""
    swid: str = ""
    year: int | None = None
    team_id: int | None = None

    @property
    def is_private(self) -> bool:
        return bool(self.espn_s2 and self.swid)

    @classmethod
    def load(cls) -> "EspnCredentials | None":
        league_id = os.environ.get("ESPN_LEAGUE_ID", "").strip()
        team_env = os.environ.get("ESPN_TEAM_ID", "").strip()
        s2 = os.environ.get("ESPN_S2", "").strip()
        swid = os.environ.get("ESPN_SWID", "").strip()
        year = os.environ.get("ESPN_YEAR", "").strip()
        if league_id.isdigit():
            return cls(int(league_id), s2, swid, int(year) if year.isdigit() else None,
                       int(team_env) if team_env.isdigit() else None)

        if CREDENTIALS_PATH.exists():
            try:
                data = json.loads(CREDENTIALS_PATH.read_text())
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read ESPN credentials: %s", exc)
                return None
            if data.get("league_id"):
                return cls(int(data["league_id"]), data.get("espn_s2", ""),
                           data.get("swid", ""), data.get("year"), data.get("team_id"))
        return None

    def save(self) -> Path:
        CREDENTIALS_PATH.write_text(json.dumps({
            "league_id": self.league_id, "espn_s2": self.espn_s2,
            "swid": self.swid, "year": self.year, "team_id": self.team_id}, indent=2))
        try:
            CREDENTIALS_PATH.chmod(0o600)   # cookies are session credentials
        except OSError:
            pass
        return CREDENTIALS_PATH


def save_roster(player_ids: list[str], team: dict | None) -> None:
    """Remember the synced roster across restarts.

    Which players are yours is a fact about your league, not about this process,
    so holding it only in memory means every restart quietly reverts My League
    and start/sit to an empty roster with no indication why.
    """
    try:
        ROSTER_PATH.write_text(json.dumps({
            "player_ids": list(player_ids), "team": team,
            "synced_at": dt.datetime.now().isoformat(timespec="seconds")}, indent=2))
        ROSTER_PATH.chmod(0o600)
    except OSError as exc:
        log.warning("could not save the ESPN roster: %s", exc)


def load_roster() -> dict:
    """The last synced roster, or empty defaults."""
    if not ROSTER_PATH.exists():
        return {"player_ids": [], "team": None, "synced_at": None}
    try:
        data = json.loads(ROSTER_PATH.read_text())
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read the saved ESPN roster: %s", exc)
        return {"player_ids": [], "team": None, "synced_at": None}
    return {"player_ids": [str(p) for p in data.get("player_ids", [])],
            "team": data.get("team"), "synced_at": data.get("synced_at")}


def _swid(value: str) -> str:
    """ESPN wants SWID wrapped in braces; accept it either way."""
    v = (value or "").strip()
    if v and not v.startswith("{"):
        v = "{" + v.strip("{}") + "}"
    return v


class EspnLeague:
    """Thin wrapper over ``espn_api.football.League``.

    Everything returns polars frames keyed on ``merge_name`` so it joins
    straight onto the projection board.
    """

    def __init__(self, credentials: EspnCredentials | None = None, year: int | None = None):
        self.credentials = credentials or EspnCredentials.load()
        if self.credentials is None:
            raise RuntimeError(
                "No ESPN league configured. Set ESPN_LEAGUE_ID (plus ESPN_S2 and "
                "ESPN_SWID for a private league), or save them in Settings."
            )
        self.year = year or self.credentials.year or current_season()
        self._league = None

    # ------------------------------------------------------------------ connect
    @property
    def league(self):
        if self._league is not None:
            return self._league
        try:
            from espn_api.football import League
        except ImportError as exc:
            raise RuntimeError(
                "ESPN support needs the client library: pip install espn_api"
            ) from exc

        c = self.credentials
        kwargs = {"league_id": int(c.league_id), "year": int(self.year)}
        if c.is_private:
            kwargs["espn_s2"] = c.espn_s2
            kwargs["swid"] = _swid(c.swid)
        try:
            self._league = League(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(self._explain(exc)) from exc
        return self._league

    def _explain(self, exc: Exception) -> str:
        """Turn the library's exceptions into something actionable."""
        name = type(exc).__name__
        msg = str(exc)
        if "AccessDenied" in name or "espn_s2" in msg:
            if not self.credentials.is_private:
                return ("This league is private. Add your espn_s2 and SWID cookies "
                        "(Settings, or the ESPN_S2 / ESPN_SWID environment variables).")
            return ("ESPN rejected those cookies. They expire — sign in to "
                    "fantasy.espn.com again and copy fresh values.")
        if "InvalidLeague" in name:
            return f"ESPN has no league {self.credentials.league_id} for {self.year}."
        if "ConnectionError" in name or "Max retries" in msg:
            return f"Could not reach ESPN: {msg[:160]}"
        return f"{name}: {msg[:200]}"

    def connect(self) -> dict:
        """Verify the connection and describe the league."""
        lg = self.league
        settings = getattr(lg, "settings", None)
        return {
            "connected": True,
            "league_id": int(self.credentials.league_id),
            "year": int(self.year),
            "name": getattr(settings, "name", "") or "",
            "teams": len(lg.teams),
            "current_week": int(getattr(lg, "current_week", 0) or 0),
            "nfl_week": int(getattr(lg, "nfl_week", 0) or 0),
            "private": self.credentials.is_private,
            "scoring_type": getattr(settings, "scoring_type", "") or "",
        }

    # ------------------------------------------------------------------- teams
    def teams(self) -> pl.DataFrame:
        rows = []
        for t in self.league.teams:
            rows.append({
                "team_id": int(getattr(t, "team_id", 0) or 0),
                "team_name": getattr(t, "team_name", ""),
                "owner": ", ".join(_owner_names(t)),
                "wins": int(getattr(t, "wins", 0) or 0),
                "losses": int(getattr(t, "losses", 0) or 0),
                "ties": int(getattr(t, "ties", 0) or 0),
                "points_for": float(getattr(t, "points_for", 0) or 0),
                "points_against": float(getattr(t, "points_against", 0) or 0),
                "standing": int(getattr(t, "standing", 0) or 0),
                "playoff_pct": float(getattr(t, "playoff_pct", 0) or 0),
                "roster_size": len(getattr(t, "roster", []) or []),
            })
        return pl.DataFrame(rows).sort("standing") if rows else pl.DataFrame()

    def rosters(self) -> pl.DataFrame:
        """Every rostered player in the league, with ESPN's own projections."""
        rows = []
        for t in self.league.teams:
            for p in getattr(t, "roster", []) or []:
                rows.append({**_player_row(p),
                             "team_id": int(getattr(t, "team_id", 0) or 0),
                             "fantasy_team": getattr(t, "team_name", ""),
                             "owner": ", ".join(_owner_names(t))})
        return pl.DataFrame(rows) if rows else pl.DataFrame()

    def free_agents(self, size: int = 100, week: int | None = None) -> pl.DataFrame:
        try:
            players = self.league.free_agents(week=week, size=size)
        except Exception as exc:  # noqa: BLE001
            log.info("free agents unavailable: %s", exc)
            return pl.DataFrame()
        rows = [{**_player_row(p), "fantasy_team": "", "team_id": 0, "owner": ""}
                for p in players]
        return pl.DataFrame(rows) if rows else pl.DataFrame()

    def my_roster(self, team_name: str | None = None,
                  team_id: int | None = None) -> pl.DataFrame:
        """One team's roster. Defaults to the team the cookies belong to."""
        rosters = self.rosters()
        if rosters.is_empty():
            return rosters
        team_id = team_id if team_id is not None else self.credentials.team_id
        if team_id is not None:
            return rosters.filter(pl.col("team_id") == int(team_id))
        if team_name:
            return rosters.filter(
                pl.col("fantasy_team").str.to_lowercase() == team_name.lower())
        # No hint given: espn-api marks the cookie owner's team when it can.
        for t in self.league.teams:
            if getattr(t, "owners", None) and getattr(self.league, "team_id", None) == t.team_id:
                return rosters.filter(pl.col("team_id") == t.team_id)
        return rosters

    # ------------------------------------------------------- weekly projections
    def week_projections(self, week: int) -> pl.DataFrame:
        """ESPN's per-week projection and actual for every started/benched player.

        Box scores are the only place ESPN exposes a *weekly* projection
        alongside what actually happened, which is what makes an honest
        head-to-head scoring possible after the fact.
        """
        try:
            boxes = self.league.box_scores(week=week)
        except Exception as exc:  # noqa: BLE001
            log.info("box scores unavailable for week %s: %s", week, exc)
            return pl.DataFrame()

        rows = []
        for box in boxes:
            for side, lineup in (("home", getattr(box, "home_lineup", []) or []),
                                 ("away", getattr(box, "away_lineup", []) or [])):
                team = getattr(box, f"{side}_team", None)
                for p in lineup:
                    rows.append({
                        **_player_row(p),
                        "week": int(week),
                        "slot": getattr(p, "slot_position", ""),
                        "started": getattr(p, "slot_position", "") not in ("BE", "IR"),
                        "espn_week_proj": float(getattr(p, "projected_points", 0) or 0),
                        "espn_week_actual": float(getattr(p, "points", 0) or 0),
                        "pro_opponent": getattr(p, "pro_opponent", ""),
                        "on_bye": bool(getattr(p, "on_bye_week", False)),
                        "fantasy_team": getattr(team, "team_name", "") if team else "",
                        "team_id": int(getattr(team, "team_id", 0) or 0) if team else 0,
                    })
        return pl.DataFrame(rows) if rows else pl.DataFrame()

    def matchups(self, week: int) -> pl.DataFrame:
        try:
            boxes = self.league.box_scores(week=week)
        except Exception as exc:  # noqa: BLE001
            log.info("matchups unavailable: %s", exc)
            return pl.DataFrame()
        rows = []
        for b in boxes:
            home, away = getattr(b, "home_team", None), getattr(b, "away_team", None)
            rows.append({
                "week": int(week),
                "home_team": getattr(home, "team_name", "") if home else "",
                "away_team": getattr(away, "team_name", "") if away else "",
                "home_score": float(getattr(b, "home_score", 0) or 0),
                "away_score": float(getattr(b, "away_score", 0) or 0),
                "home_projected": float(getattr(b, "home_projected", 0) or 0),
                "away_projected": float(getattr(b, "away_projected", 0) or 0),
            })
        return pl.DataFrame(rows) if rows else pl.DataFrame()

    def import_settings(self) -> dict:
        """Read your league's real settings so you never type them twice.

        ESPN knows how many of each slot you start and what a reception is
        worth; both drive replacement level, auction values and the
        recommender, so importing them removes the most common way those
        numbers end up quietly wrong.
        """
        lg = self.league
        settings = getattr(lg, "settings", None)
        if settings is None:
            return {}

        roster: dict[str, int] = {}
        for slot, count in (getattr(settings, "position_slot_counts", {}) or {}).items():
            key = SLOT_FIX.get(slot)
            if key and int(count) > 0:
                roster[key] = roster.get(key, 0) + int(count)
        # Positions ESPN lists but nobody starts just clutter the board.
        roster = {k: v for k, v in roster.items() if v > 0}

        scoring = {item.get("id"): float(item.get("points", 0) or 0)
                   for item in (getattr(settings, "scoring_format", []) or [])}
        rec = scoring.get(STAT_RECEPTION, 0.0)
        preset = "ppr" if rec >= 0.75 else ("half_ppr" if rec >= 0.25 else "standard")

        out = {
            "name": getattr(settings, "name", "") or f"ESPN league {c_id(self)}",
            "teams": len(lg.teams) or 12,
            "scoring_preset": preset,
            "roster": roster or None,
            "reception_points": rec,
            "passing_td_points": scoring.get(STAT_PASS_TD, 4.0),
        }
        # Only override individual values ESPN genuinely disagrees with the preset on.
        overrides = {}
        if rec and abs(rec - {"ppr": 1.0, "half_ppr": 0.5, "standard": 0.0}[preset]) > 1e-9:
            overrides["receptions"] = rec
        if scoring.get(STAT_PASS_TD) and abs(scoring[STAT_PASS_TD] - 4.0) > 1e-9:
            overrides["passing_tds"] = scoring[STAT_PASS_TD]
        if scoring.get(STAT_PASS_YDS) and abs(scoring[STAT_PASS_YDS] - 0.04) > 1e-9:
            overrides["passing_yards"] = scoring[STAT_PASS_YDS]
        out["scoring_overrides"] = overrides
        return out

    def my_team(self) -> dict:
        """Which team is yours, if the credentials identify one."""
        target = self.credentials.team_id
        for t in self.league.teams:
            if target and int(getattr(t, "team_id", 0)) == int(target):
                return {"team_id": int(t.team_id), "team_name": t.team_name,
                        "owner": ", ".join(_owner_names(t))}
        return {}

    def schedule(self) -> pl.DataFrame:
        """Your fantasy schedule: opponent and result per week."""
        me = self.credentials.team_id
        if not me:
            return pl.DataFrame()
        team = next((t for t in self.league.teams
                     if int(getattr(t, "team_id", 0)) == int(me)), None)
        if team is None:
            return pl.DataFrame()
        rows = []
        opponents = getattr(team, "schedule", []) or []
        scores = getattr(team, "scores", []) or []
        outcomes = getattr(team, "outcomes", []) or []
        for i, opp in enumerate(opponents):
            rows.append({
                "week": i + 1,
                "opponent": getattr(opp, "team_name", ""),
                "opponent_id": int(getattr(opp, "team_id", 0) or 0),
                "my_score": float(scores[i]) if i < len(scores) else 0.0,
                "outcome": outcomes[i] if i < len(outcomes) else "",
            })
        return pl.DataFrame(rows) if rows else pl.DataFrame()

    def draft(self) -> pl.DataFrame:
        picks = getattr(self.league, "draft", None) or []
        rows = []
        for pick in picks:
            player = getattr(pick, "playerName", "")
            rows.append({
                "round": int(getattr(pick, "round_num", 0) or 0),
                "pick": int(getattr(pick, "round_pick", 0) or 0),
                "player_name": player,
                "merge_name": normalize_name(player),
                "fantasy_team": getattr(getattr(pick, "team", None), "team_name", ""),
                "bid_amount": float(getattr(pick, "bid_amount", 0) or 0),
                "keeper": bool(getattr(pick, "keeper_status", False)),
            })
        return pl.DataFrame(rows) if rows else pl.DataFrame()


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
def _owner_names(team) -> list[str]:
    """espn-api has returned owners as strings and as dicts across versions."""
    out = []
    for o in getattr(team, "owners", []) or []:
        if isinstance(o, dict):
            name = f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()
            out.append(name or o.get("displayName", "") or o.get("id", ""))
        else:
            out.append(str(o))
    return [o for o in out if o]


def _player_row(p) -> dict:
    """Normalise an espn-api Player/BoxPlayer into our vocabulary."""
    name = getattr(p, "name", "") or ""
    pos = getattr(p, "position", "") or ""
    pro = getattr(p, "proTeam", "") or ""
    return {
        "espn_id": int(getattr(p, "playerId", 0) or 0),
        "player_name": name,
        "merge_name": normalize_name(name),
        "position": POSITION_FIX.get(pos, pos),
        "pro_team": TEAM_FIX.get(pro, pro),
        "espn_proj_total": float(getattr(p, "projected_total_points", 0) or 0),
        "espn_proj_avg": float(getattr(p, "projected_avg_points", 0) or 0),
        "espn_actual_total": float(getattr(p, "total_points", 0) or 0),
        "espn_actual_avg": float(getattr(p, "avg_points", 0) or 0),
        "percent_owned": float(getattr(p, "percent_owned", 0) or 0),
        "percent_started": float(getattr(p, "percent_started", 0) or 0),
        "injured": bool(getattr(p, "injured", False)),
        "injury_status": getattr(p, "injuryStatus", "") or "",
    }


def espn_available() -> bool:
    try:
        import espn_api  # noqa: F401
        return True
    except ImportError:
        return False


def c_id(lg: "EspnLeague") -> int:
    return int(lg.credentials.league_id)
