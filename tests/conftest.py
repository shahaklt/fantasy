import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import polars as pl
import pytest

from gridiron.scoring import LeagueSettings


@pytest.fixture
def league():
    return LeagueSettings(teams=12, draft_slot=5)


@pytest.fixture
def fake_board():
    """A small deterministic board, so draft tests never touch the network."""
    rng = np.random.default_rng(0)
    rows = []
    pid = 0
    for pos, count, base in (("QB", 24, 260), ("RB", 60, 250), ("WR", 80, 240),
                             ("TE", 30, 180), ("K", 20, 140), ("DST", 20, 120)):
        for i in range(count):
            pid += 1
            pts = base * (0.985 ** i) + rng.normal(0, 3)
            rows.append({
                "player_id": f"p{pid:04d}",
                "player_name": f"{pos} Player {i + 1}",
                "position": pos,
                "team": f"T{(pid % 32) + 1:02d}",
                "proj_points": float(pts),
                "proj_ppg": float(pts / 16),
                "proj_games": 15.5,
                "adp": float(pid),
                "adp_sd": 8.0,
            })
    return pl.DataFrame(rows)
