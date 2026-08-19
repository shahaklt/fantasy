"""League scoring + roster configuration and fantasy point arithmetic."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Mapping

import numpy as np

# Positions that can legally fill each lineup slot.
SLOT_ELIGIBILITY: dict[str, tuple[str, ...]] = {
    "QB": ("QB",),
    "RB": ("RB",),
    "WR": ("WR",),
    "TE": ("TE",),
    "FLEX": ("RB", "WR", "TE"),
    "WRRB_FLEX": ("RB", "WR"),
    "REC_FLEX": ("WR", "TE"),
    "SUPERFLEX": ("QB", "RB", "WR", "TE"),
    "OP": ("QB", "RB", "WR", "TE"),
    "K": ("K",),
    "DST": ("DST",),
    "BN": ("QB", "RB", "WR", "TE", "K", "DST"),
    "IR": ("QB", "RB", "WR", "TE", "K", "DST"),
}

STARTING_SLOTS = ("QB", "RB", "WR", "TE", "FLEX", "WRRB_FLEX", "REC_FLEX", "SUPERFLEX", "OP", "K", "DST")


@dataclass
class Scoring:
    """Per-event point values. Defaults are standard half-PPR."""

    passing_yards: float = 0.04
    passing_tds: float = 4.0
    passing_interceptions: float = -2.0
    passing_2pt_conversions: float = 2.0
    pass_400_bonus: float = 0.0
    pass_300_bonus: float = 0.0

    rushing_yards: float = 0.1
    rushing_tds: float = 6.0
    rushing_2pt_conversions: float = 2.0
    rush_100_bonus: float = 0.0

    receptions: float = 0.5
    receiving_yards: float = 0.1
    receiving_tds: float = 6.0
    receiving_2pt_conversions: float = 2.0
    rec_100_bonus: float = 0.0
    te_premium: float = 0.0  # extra points per TE reception

    fumbles_lost: float = -2.0

    # Kicker
    fg_0_39: float = 3.0
    fg_40_49: float = 4.0
    fg_50_plus: float = 5.0
    fg_miss: float = -1.0
    pat_made: float = 1.0

    # DST
    dst_sack: float = 1.0
    dst_interception: float = 2.0
    dst_fumble_recovery: float = 2.0
    dst_td: float = 6.0
    dst_safety: float = 2.0
    dst_points_allowed_0: float = 10.0
    dst_points_allowed_1_6: float = 7.0
    dst_points_allowed_7_13: float = 4.0
    dst_points_allowed_14_20: float = 1.0
    dst_points_allowed_21_27: float = 0.0
    dst_points_allowed_28_34: float = -1.0
    dst_points_allowed_35_plus: float = -4.0

    @classmethod
    def preset(cls, name: str) -> "Scoring":
        name = (name or "half_ppr").lower().replace("-", "_")
        if name in ("ppr", "full_ppr"):
            return cls(receptions=1.0)
        if name in ("standard", "std", "non_ppr"):
            return cls(receptions=0.0)
        if name in ("te_premium", "tep"):
            return cls(receptions=1.0, te_premium=0.5)
        if name in ("draftkings", "dk"):
            return cls(receptions=1.0, pass_300_bonus=3.0, rush_100_bonus=3.0,
                       rec_100_bonus=3.0, passing_yards=0.04, fumbles_lost=-1.0)
        return cls()  # half PPR

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass
class LeagueSettings:
    """Everything the engine needs to know about your league."""

    name: str = "My League"
    teams: int = 12
    scoring: Scoring | None = None
    scoring_preset: str = "half_ppr"
    roster: dict[str, int] = field(
        default_factory=lambda: {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "K": 1, "DST": 1, "BN": 6}
    )
    draft_type: str = "snake"  # snake | auction | linear
    draft_slot: int = 1  # 1-indexed position in round 1
    auction_budget: int = 200
    playoff_teams: int = 6
    playoff_start_week: int = 15
    regular_season_weeks: int = 14
    keeper_player_ids: list[str] = field(default_factory=list)

    def __post_init__(self):
        # Resolve the preset unless an explicit Scoring was supplied. Without
        # this, LeagueSettings(scoring_preset="ppr") silently scored half PPR.
        if self.scoring is None:
            self.scoring = Scoring.preset(self.scoring_preset)

    # ---------------------------------------------------------------- derived
    @property
    def starters(self) -> dict[str, int]:
        return {k: v for k, v in self.roster.items() if k in STARTING_SLOTS and v > 0}

    @property
    def roster_size(self) -> int:
        return int(sum(self.roster.values()))

    @property
    def bench_size(self) -> int:
        return int(self.roster.get("BN", 0))

    @property
    def total_picks(self) -> int:
        return self.teams * self.roster_size

    def starters_per_team(self) -> int:
        return int(sum(self.starters.values()))

    def positional_demand(self) -> dict[str, float]:
        """Expected number of *starters* drafted league-wide at each position.

        FLEX demand is split across eligible positions using empirical usage
        weights (RB/WR carry most flex snaps; TE rarely).
        """
        flex_weights = {"FLEX": {"RB": 0.42, "WR": 0.5, "TE": 0.08},
                        "WRRB_FLEX": {"RB": 0.45, "WR": 0.55},
                        "REC_FLEX": {"WR": 0.8, "TE": 0.2},
                        "SUPERFLEX": {"QB": 0.85, "RB": 0.05, "WR": 0.08, "TE": 0.02},
                        "OP": {"QB": 0.85, "RB": 0.05, "WR": 0.08, "TE": 0.02}}
        demand: dict[str, float] = {p: 0.0 for p in ("QB", "RB", "WR", "TE", "K", "DST")}
        for slot, count in self.starters.items():
            if slot in flex_weights:
                for pos, w in flex_weights[slot].items():
                    demand[pos] += count * w * self.teams
            else:
                for pos in SLOT_ELIGIBILITY.get(slot, ()):  # single-position slots
                    demand[pos] += count * self.teams
        return demand

    def to_dict(self) -> dict[str, Any]:
        """Full settings, with every scoring value expanded (for API clients)."""
        d = asdict(self)
        d["scoring"] = self.scoring.to_dict()
        return d

    def to_config(self) -> dict[str, Any]:
        """Canonical form for persistence: scoring stored only where it differs.

        Writing the expanded values back would make the preset unchangeable --
        the saved block would keep overriding the new preset on load, so
        switching from half PPR to PPR would change the label and nothing else.
        """
        d = asdict(self)
        preset = Scoring.preset(self.scoring_preset).to_dict()
        overrides = {k: v for k, v in self.scoring.to_dict().items()
                     if abs(float(v) - float(preset.get(k, 0.0))) > 1e-12}
        if overrides:
            d["scoring"] = overrides
        else:
            d.pop("scoring", None)
        return d

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LeagueSettings":
        data = dict(data)
        scoring = data.pop("scoring", None)
        preset = data.get("scoring_preset", "half_ppr")
        obj = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        if isinstance(scoring, Mapping):
            base = Scoring.preset(preset)
            for k, v in scoring.items():
                if hasattr(base, k) and v is not None:
                    setattr(base, k, float(v))
            obj.scoring = base
        else:
            obj.scoring = Scoring.preset(preset)
        return obj


# --------------------------------------------------------------------------------------
# Fantasy point arithmetic
# --------------------------------------------------------------------------------------
#: Stat columns the simulator produces, in canonical order.
STAT_COLUMNS = (
    "passing_yards", "passing_tds", "passing_interceptions",
    "carries", "rushing_yards", "rushing_tds",
    "receptions", "targets", "receiving_yards", "receiving_tds",
    "fumbles_lost",
)


def score_array(stats: Mapping[str, np.ndarray], scoring: Scoring, is_te: np.ndarray | None = None) -> np.ndarray:
    """Vectorised fantasy points from a mapping of stat name -> array."""
    def g(name: str):
        v = stats.get(name)
        return 0.0 if v is None else v

    pts = (
        g("passing_yards") * scoring.passing_yards
        + g("passing_tds") * scoring.passing_tds
        + g("passing_interceptions") * scoring.passing_interceptions
        + g("rushing_yards") * scoring.rushing_yards
        + g("rushing_tds") * scoring.rushing_tds
        + g("receptions") * scoring.receptions
        + g("receiving_yards") * scoring.receiving_yards
        + g("receiving_tds") * scoring.receiving_tds
        + g("fumbles_lost") * scoring.fumbles_lost
        + g("passing_2pt_conversions") * scoring.passing_2pt_conversions
        + g("rushing_2pt_conversions") * scoring.rushing_2pt_conversions
        + g("receiving_2pt_conversions") * scoring.receiving_2pt_conversions
    )
    if scoring.pass_300_bonus:
        pts = pts + (g("passing_yards") >= 300) * scoring.pass_300_bonus
    if scoring.pass_400_bonus:
        pts = pts + (g("passing_yards") >= 400) * scoring.pass_400_bonus
    if scoring.rush_100_bonus:
        pts = pts + (g("rushing_yards") >= 100) * scoring.rush_100_bonus
    if scoring.rec_100_bonus:
        pts = pts + (g("receiving_yards") >= 100) * scoring.rec_100_bonus
    if scoring.te_premium and is_te is not None:
        pts = pts + g("receptions") * scoring.te_premium * is_te
    return pts


def score_expression(scoring: Scoring):
    """Polars expression scoring an nflverse ``player_stats`` frame."""
    import polars as pl

    def c(name: str):
        return pl.col(name).fill_null(0.0) if name else pl.lit(0.0)

    expr = (
        c("passing_yards") * scoring.passing_yards
        + c("passing_tds") * scoring.passing_tds
        + c("passing_interceptions") * scoring.passing_interceptions
        + c("rushing_yards") * scoring.rushing_yards
        + c("rushing_tds") * scoring.rushing_tds
        + c("receptions") * scoring.receptions
        + c("receiving_yards") * scoring.receiving_yards
        + c("receiving_tds") * scoring.receiving_tds
        + (c("rushing_fumbles_lost") + c("receiving_fumbles_lost") + c("sack_fumbles_lost")) * scoring.fumbles_lost
        + c("passing_2pt_conversions") * scoring.passing_2pt_conversions
        + c("rushing_2pt_conversions") * scoring.rushing_2pt_conversions
        + c("receiving_2pt_conversions") * scoring.receiving_2pt_conversions
    )
    if scoring.pass_300_bonus:
        expr = expr + (c("passing_yards") >= 300).cast(pl.Float64) * scoring.pass_300_bonus
    if scoring.pass_400_bonus:
        expr = expr + (c("passing_yards") >= 400).cast(pl.Float64) * scoring.pass_400_bonus
    if scoring.rush_100_bonus:
        expr = expr + (c("rushing_yards") >= 100).cast(pl.Float64) * scoring.rush_100_bonus
    if scoring.rec_100_bonus:
        expr = expr + (c("receiving_yards") >= 100).cast(pl.Float64) * scoring.rec_100_bonus
    if scoring.te_premium:
        expr = expr + c("receptions") * scoring.te_premium * (pl.col("position") == "TE").cast(pl.Float64)
    return expr.alias("fantasy_points")
