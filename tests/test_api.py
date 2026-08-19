"""API smoke tests.

These run against the real app with a tiny simulation budget, so they exercise
the whole stack (data -> projections -> simulation -> HTTP) rather than mocks.
They need a warm data cache; they skip rather than fail when data is
unavailable, so the suite still runs offline.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from gridiron import pipeline
from gridiron.api.server import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def built(client):
    """Build a small artifact set once for the module."""
    try:
        pipeline.build_all(n_sims=200, keep_weekly=True)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"data unavailable: {exc}")
    return True


def test_status_reports_the_compute_backend(client):
    body = client.get("/api/status").json()
    assert "backend" in body["compute"]
    assert body["trading_mode"] in ("paper", "live")
    assert isinstance(body["scheduler"]["times"], list)


def test_scheduler_lists_the_refresh_jobs(client):
    jobs = client.get("/api/scheduler").json()["jobs"]
    assert "refresh_data" in jobs and "rebuild_projections" in jobs


def test_players_endpoint_returns_a_ranked_board(client, built):
    body = client.get("/api/players?limit=25").json()
    assert body["count"] > 100
    points = [p["proj_points"] for p in body["players"]]
    assert points == sorted(points, reverse=True)


def test_position_filter_is_respected(client, built):
    body = client.get("/api/players?position=QB&limit=10").json()
    assert {p["position"] for p in body["players"]} == {"QB"}


def test_player_detail_carries_a_distribution(client, built):
    top = client.get("/api/players?limit=1").json()["players"][0]
    detail = client.get(f"/api/players/{top['player_id']}").json()
    assert detail["player_name"] == top["player_name"]
    assert len(detail["distribution"]["counts"]) > 5
    assert len(detail["weekly"]) >= 17


def test_unknown_player_is_a_404(client, built):
    assert client.get("/api/players/not-a-real-id").status_code == 404


def test_draft_pick_and_undo_round_trip(client, built):
    client.post("/api/draft/reset", json={})
    board = client.get("/api/draft/board?limit=5").json()
    first = board["players"][0]["player_id"]
    after = client.post("/api/draft/pick", json={"player_id": first}).json()
    assert after["pick"] == 2

    # The same player cannot go twice, and he leaves the board.
    assert client.post("/api/draft/pick", json={"player_id": first}).status_code == 400
    ids = [p["player_id"] for p in client.get("/api/draft/board?limit=50").json()["players"]]
    assert first not in ids

    assert client.post("/api/draft/undo").json()["pick"] == 1


def test_scarcity_covers_every_position(client, built):
    rows = client.get("/api/draft/scarcity").json()
    assert {r["position"] for r in rows} >= {"QB", "RB", "WR", "TE"}
    assert all(r["drop_12"] >= 0 for r in rows)


def test_games_are_coherent_and_priced_against_the_market(client, built):
    body = client.get("/api/games?week=1&n_sims=800").json()
    assert len(body["games"]) >= 12
    for g in body["games"]:
        assert g["home_win_prob"] + g["away_win_prob"] == pytest.approx(1.0, abs=0.02)
        assert 20 < g["proj_total"] < 80
        assert g["proj_home_score"] > 0


def test_game_detail_breaks_down_both_teams(client, built):
    games = client.get("/api/games?week=1&n_sims=800").json()["games"]
    detail = client.get(f"/api/games/{games[0]['game_id']}?week=1&n_sims=800").json()
    assert set(detail) >= {"summary", "distribution", "alt_lines", "home_breakdown", "away_breakdown"}
    home = detail["home_breakdown"]
    assert home["totals"]["fantasy_points"] > 0
    assert len(home["players"]) > 3
    # Alternate lines must be monotone: a higher total is harder to go over.
    overs = [t["over"] for t in detail["alt_lines"]["totals"]]
    assert overs == sorted(overs, reverse=True)


def test_lineup_optimiser_ranks_starters(client, built):
    ids = [p["player_id"] for p in client.get("/api/players?limit=12").json()["players"]]
    rows = client.post("/api/lineup", json={"player_ids": ids, "week": 1}).json()
    assert len(rows) == len(ids)
    assert all(0.0 <= r["start_pct"] <= 1.0 for r in rows)
    assert rows[0]["start_pct"] >= rows[-1]["start_pct"]


def test_league_simulation_produces_probabilities(client, built):
    players = client.get("/api/players?limit=40").json()["players"]
    teams = {f"Team {i}": [p["player_id"] for p in players[i::4]] for i in range(4)}
    body = client.post("/api/league/simulate", json={"teams": teams}).json()
    standings = body["standings"]
    assert len(standings) == 4
    assert sum(t["title_odds"] for t in standings) == pytest.approx(1.0, abs=0.05)


def test_league_settings_can_be_updated(client):
    original = client.get("/api/league").json()
    client.post("/api/league", json={"teams": 14, "scoring_preset": "ppr"})
    updated = client.get("/api/league").json()
    assert updated["teams"] == 14
    assert updated["scoring"]["receptions"] == 1.0
    client.post("/api/league", json={"teams": original["teams"],
                                     "scoring_preset": original["scoring_preset"]})


def test_portfolio_starts_in_paper_mode(client):
    body = client.get("/api/portfolio").json()
    assert body["mode"] == "paper"
    assert body["paper"]["cash"] > 0
    assert body["risk"]["limits"]["max_order_notional"] > 0
