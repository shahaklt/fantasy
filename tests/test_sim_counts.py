"""One run count, wherever you read it.

These numbers had drifted to five different values across the call sites, so
"how many runs is this?" depended on which screen you were looking at. There is
one constant now; these tests keep it that way, including across the Python /
JavaScript boundary where nothing else would catch a mismatch.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from gridiron.config import DEFAULT_SIMS, DRAFT_SIMS

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "web" / "assets"


def test_the_default_is_twenty_thousand():
    assert DEFAULT_SIMS == 20_000


def test_the_draft_budget_stays_inside_a_pick_timer():
    """Measured at ~90ms per run over a 12-candidate slate on four cores.

    The recommender walks the remainder of the draft in Python once per run per
    candidate, so it is roughly 1,200x the cost of a season run and cannot share
    the headline number — 20,000 would be half an hour with a clock going.
    """
    assert DRAFT_SIMS <= 2_000, "a recommendation has to return inside a pick timer"


def _js_const(name: str) -> int:
    source = (WEB / "ui.js").read_text()
    match = re.search(rf"export const {name} = (\d+);", source)
    assert match, f"{name} is not exported from ui.js"
    return int(match.group(1))


@pytest.mark.parametrize("name,expected", [("SIMS", DEFAULT_SIMS), ("DRAFT_SIMS", DRAFT_SIMS)])
def test_the_frontend_agrees_with_the_backend(name, expected):
    """Nothing else would catch this: the UI would just quietly ask for less."""
    assert _js_const(name) == expected


def test_no_view_hardcodes_its_own_run_count():
    """A literal here is how the numbers drifted apart the first time."""
    offenders = []
    for path in sorted(WEB.glob("*.js")):
        if path.name == "ui.js":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"n_sims[=:]\s*\d", line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, "use the SIMS / DRAFT_SIMS constants:\n" + "\n".join(offenders)


def test_the_request_schemas_use_the_shared_defaults():
    from gridiron.api.schemas import EspnScoreRequest, RebuildRequest, SignalRequest

    assert RebuildRequest().n_sims == DEFAULT_SIMS
    assert SignalRequest().n_sims == DEFAULT_SIMS
    assert EspnScoreRequest().n_sims == DEFAULT_SIMS


def test_a_recommendation_request_defaults_to_the_draft_budget():
    from gridiron.api.schemas import RecommendRequest

    assert RecommendRequest().n_sims == DRAFT_SIMS
