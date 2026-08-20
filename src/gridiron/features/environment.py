"""What the weather does to a football game, measured rather than assumed.

Every coefficient here was fitted from nflverse history and can be refitted
with ``gridiron calibrate``. Three findings shape the design:

1. **Wind is not priced into the closing total.** With team-season fixed
   effects, ten more miles per hour costs 1.83 points against the market line
   (significant). Since this model anchors its team totals to that line, the
   residual is exactly what may be added without double counting.

2. **Cold is already priced.** The same fit gives -0.07 points per ten degrees
   against the line, comfortably inside noise. Adjusting the total for
   temperature would be counting it twice. But cold clearly moves the *mix* --
   rush share +0.007, completion rate -0.7pp per ten degrees -- and the total
   line says nothing about the mix. So temperature adjusts composition only.

3. **Altitude does nothing measurable to kicking.** Fitted on 12,787 field
   goals with distance controlled, elevation is worth -0.01 yards per 1,000 ft
   (se 0.028) -- indistinguishable from zero, and Denver's attempts average
   39.2 yards against 38.7 elsewhere. The thin-air story is real physics and
   too small to find at this sample size, so there is no altitude knob. It is
   recorded here so the next person knows it was tested, not overlooked.

Fitted on 3,341 outdoor team-games (weather) and 8,372 outdoor field goals.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass

from ..config import ARTIFACT_DIR

log = logging.getLogger(__name__)

EFFECTS_PATH = ARTIFACT_DIR / "environment_effects.json"

# Conditions a team is used to. Effects are applied as deviations from these,
# so a 60F, 6mph game is a no-op rather than a small nudge.
BASE_TEMP_F = 60.0
BASE_WIND_MPH = 6.0


@dataclass
class EnvironmentEffects:
    """Per-unit effects, all measured with team-season fixed effects.

    Wind terms are per mph, temperature terms per degree F below BASE_TEMP_F.
    """

    # --- level: only what the market line does not already contain ---
    wind_points_per_mph: float = -0.183       # -1.83 pts per 10 mph, significant
    cold_points_per_degree: float = 0.0       # measured -0.007/F, not significant: priced

    # --- composition: the total line says nothing about the mix ---
    wind_rush_share_per_mph: float = 0.00167  # +0.0167 per 10 mph
    cold_rush_share_per_degree: float = 0.00071
    wind_comp_pct_per_mph: float = -0.00152   # -1.5pp per 10 mph
    cold_comp_pct_per_degree: float = -0.00066
    wind_ypa_per_mph: float = -0.0324
    cold_ypa_per_degree: float = -0.00819

    # --- kicking: effective yards of distance lost ---
    wind_kick_yards_per_mph: float = -0.163   # -1.63 yd per 10 mph, significant
    altitude_kick_yards_per_1000ft: float = 0.0   # measured -0.01, not significant

    # --- precipitation: not in nflverse, so not fitted. Off unless a forecast
    #     supplies it, and even then held to a modest, clearly-labelled prior.
    precip_rush_share: float = 0.010
    precip_comp_pct: float = -0.010

    # Clamp: extrapolating a linear fit into a hurricane is not evidence.
    max_wind_mph: float = 25.0
    min_temp_f: float = 10.0

    fitted_at: str = ""
    n_team_games: int = 3341
    n_field_goals: int = 8372

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls) -> "EnvironmentEffects":
        """Fitted values if calibration has run, otherwise the measured defaults."""
        if EFFECTS_PATH.exists():
            try:
                return cls(**json.loads(EFFECTS_PATH.read_text()))
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read fitted environment effects: %s", exc)
        return cls()

    def save(self) -> None:
        EFFECTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        EFFECTS_PATH.write_text(json.dumps(self.as_dict(), indent=2))


@dataclass
class GameAdjustment:
    """What the conditions do to one game, in units the projection speaks."""

    total_points: float = 0.0        # added to the expected game total
    rush_share: float = 0.0          # added to the offense's rushing share
    comp_pct: float = 0.0            # added to completion rate
    ypa: float = 0.0                 # added to yards per attempt
    kick_yards: float = 0.0          # effective FG distance gained/lost
    reason: str = "neutral"
    indoor: bool = False

    @property
    def is_neutral(self) -> bool:
        """Nothing to apply — either ordinary conditions or nothing known."""
        return not any((self.total_points, self.rush_share, self.comp_pct,
                        self.ypa, self.kick_yards))

    def as_dict(self) -> dict:
        return {**asdict(self), "is_neutral": self.is_neutral}


def adjust(weather, effects: EnvironmentEffects | None = None) -> GameAdjustment:
    """Turn conditions into projection deltas.

    Indoors returns exactly neutral rather than a small nudge: a closed roof is
    a known constant, and the model's baselines already include dome games.
    """
    fx = effects or EnvironmentEffects.load()

    if getattr(weather, "indoor", False):
        return GameAdjustment(reason="indoor (closed roof)", indoor=True)
    if getattr(weather, "source", "") == "default":
        # No reading and no forecast: do not invent one.
        return GameAdjustment(reason="no forecast available")

    wind = min(max(float(getattr(weather, "wind_mph", 0.0)), 0.0), fx.max_wind_mph)
    temp = max(float(getattr(weather, "temp_f", BASE_TEMP_F)), fx.min_temp_f)
    precip = float(getattr(weather, "precip_chance", 0.0) or 0.0)

    d_wind = max(wind - BASE_WIND_MPH, 0.0)      # only *above* an ordinary breeze
    d_cold = max(BASE_TEMP_F - temp, 0.0)        # only *below* an ordinary day

    adj = GameAdjustment(
        total_points=fx.wind_points_per_mph * d_wind + fx.cold_points_per_degree * d_cold,
        rush_share=(fx.wind_rush_share_per_mph * d_wind
                    + fx.cold_rush_share_per_degree * d_cold
                    + fx.precip_rush_share * precip),
        comp_pct=(fx.wind_comp_pct_per_mph * d_wind
                  + fx.cold_comp_pct_per_degree * d_cold
                  + fx.precip_comp_pct * precip),
        ypa=fx.wind_ypa_per_mph * d_wind + fx.cold_ypa_per_degree * d_cold,
        kick_yards=(fx.wind_kick_yards_per_mph * d_wind
                    + fx.altitude_kick_yards_per_1000ft
                    * float(getattr(weather, "elevation_ft", 0.0)) / 1000.0),
    )

    parts = []
    if d_wind > 2:
        parts.append(f"{wind:.0f} mph wind")
    if d_cold > 5:
        parts.append(f"{temp:.0f}°F")
    if precip > 0.4:
        parts.append(f"{precip:.0%} rain")
    adj.reason = ", ".join(parts) if parts else "neutral"
    return adj


def describe(adj: GameAdjustment) -> str:
    """One line a human can check against their own intuition."""
    if adj.indoor:
        return "Indoors — no weather effect."
    if adj.reason == "no forecast available":
        return "No forecast yet — projected on neutral conditions."
    if adj.is_neutral:
        return "Ordinary conditions — no adjustment."
    bits = [f"{adj.total_points:+.1f} pts to the game total",
            f"{adj.rush_share:+.1%} rushing share",
            f"{adj.comp_pct:+.1%} completion rate"]
    if abs(adj.kick_yards) > 0.3:
        bits.append(f"{adj.kick_yards:+.1f} yd of field-goal range")
    return f"{adj.reason}: " + ", ".join(bits)
