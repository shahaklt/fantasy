"""Calibrated simulation constants.

Every number here was estimated from nflverse data (play-by-play 2023-2025,
team/player weekly 2021-2025) by ``scripts/calibrate.py``. If that script is
re-run it writes ``data/artifacts/calibration.json``, which overrides these
defaults at import time -- so the engine improves as new seasons land without a
code change.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields

from ..config import ARTIFACT_DIR

log = logging.getLogger(__name__)

CALIBRATION_PATH = ARTIFACT_DIR / "calibration.json"


@dataclass
class Calibration:
    # --- game outcome (residuals around the closing line) ---
    margin_sd: float = 12.73          # sd of final margin around the spread
    total_sd: float = 13.19           # sd of final total around the total line
    margin_total_corr: float = 0.025  # essentially independent, but kept explicit

    # --- team volume ---
    team_strength_sd: float = 2.60    # season-long error in a team's expected ppg
    plays_sd: float = 8.29            # sd of team offensive plays about expectation
    plays_per_margin: float = 0.086   # extra plays per point of realised margin
    pass_rate_per_margin: float = -0.0043   # game script: trailing teams throw
    pass_rate_sd: float = 0.089       # residual sd of team pass rate after game script
    league_sack_rate: float = 0.0693

    # --- scoring ---
    td_intercept: float = -0.4415     # offensive TDs = a + b * team points
    td_slope: float = 0.1250
    td_sd: float = 0.637
    pass_td_share: float = 0.6149     # share of offensive TDs that come via the pass
    pass_td_share_logit_sd: float = 0.25  # season-long team variation in that split
    role_shock_floor: float = 0.13    # season role uncertainty for a locked-in starter
    role_shock_span: float = 0.26     # extra role uncertainty when the role is unknown
    efficiency_shock_sd: float = 0.11 # season-long efficiency surprise
    two_point_rate: float = 0.021     # 2-pt conversions per TD

    # --- usage dispersion (Dirichlet concentration) ---
    target_share_kappa: float = 25.0
    carry_share_kappa: float = 11.0
    dropback_share_kappa: float = 120.0   # QB snaps are close to deterministic

    # --- per-play yardage: shifted gamma, y = Gamma(k, theta) - shift ---
    rec_yards_shape: float = 2.084
    rec_yards_shift: float = 3.0
    rush_yards_shape: float = 1.552
    rush_yards_shift: float = 3.0

    # --- kicking ---
    fg_att_intercept: float = 1.632
    fg_att_slope: float = 0.0150
    pat_rate: float = 0.960

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def load_calibration() -> Calibration:
    """Committed defaults, overridden by a locally-fitted calibration if present."""
    cal = Calibration()
    if not CALIBRATION_PATH.exists():
        return cal
    try:
        data = json.loads(CALIBRATION_PATH.read_text())
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read %s: %s", CALIBRATION_PATH, exc)
        return cal
    valid = {f.name for f in fields(Calibration)}
    for k, v in data.items():
        if k in valid and isinstance(v, (int, float)):
            setattr(cal, k, float(v))
    log.info("loaded local calibration from %s", CALIBRATION_PATH)
    return cal


CALIBRATION = load_calibration()
