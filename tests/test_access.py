"""The gate that stands between a phone on the wifi and the trading endpoints.

Everything here is about one question: can a device that does not have the
token reach anything? The answer has to stay no for HTTP, for the websocket,
and for a request that arrives over a tunnel wearing a loopback address.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from gridiron import net
from gridiron.api import access
from gridiron.api.server import app


@pytest.fixture
def guarded(tmp_path, monkeypatch):
    """Guard on, with a throwaway token that never touches the real one."""
    monkeypatch.delenv("GRIDIRON_TOKEN", raising=False)
    monkeypatch.setattr(access, "TOKEN_PATH", tmp_path / "access_token")
    monkeypatch.setattr(access.settings, "required", True)
    token = access.access_token()
    yield token
    monkeypatch.setattr(access.settings, "required", False)


def local_client() -> TestClient:
    """The browser on the machine running the server."""
    return TestClient(app, client=("127.0.0.1", 50000))


def phone_client() -> TestClient:
    """Another device on the same wifi."""
    return TestClient(app, client=("192.168.1.77", 51234))


# --------------------------------------------------------------------- token store
def test_token_is_stable_and_private(tmp_path, monkeypatch):
    monkeypatch.delenv("GRIDIRON_TOKEN", raising=False)
    monkeypatch.setattr(access, "TOKEN_PATH", tmp_path / "access_token")
    first = access.access_token()
    assert first and first == access.access_token()
    mode = (tmp_path / "access_token").stat().st_mode & 0o077
    assert mode == 0, "the access token must not be group or world readable"


def test_env_token_wins_and_cannot_be_rotated(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "TOKEN_PATH", tmp_path / "access_token")
    monkeypatch.setenv("GRIDIRON_TOKEN", "pinned-value")
    assert access.access_token() == "pinned-value"
    with pytest.raises(RuntimeError):
        access.rotate_token()


def test_rotation_invalidates_the_old_token(tmp_path, monkeypatch):
    monkeypatch.delenv("GRIDIRON_TOKEN", raising=False)
    monkeypatch.setattr(access, "TOKEN_PATH", tmp_path / "access_token")
    old = access.access_token()
    assert access.rotate_token() != old


# ------------------------------------------------------------------------ the gate
def test_loopback_is_never_challenged(guarded):
    with local_client() as client:
        assert client.get("/api/status").status_code == 200


def test_an_unrecognisable_peer_is_not_treated_as_local(guarded):
    """Fail closed: anything we cannot prove is loopback is remote."""
    with TestClient(app, client=("not-an-address", 1)) as client:
        assert client.get("/api/status").status_code == 401


def test_remote_without_a_token_is_refused(guarded):
    with phone_client() as client:
        assert client.get("/api/status").status_code == 401


def test_remote_with_the_token_gets_through(guarded):
    with phone_client() as client:
        assert client.get("/api/status", headers={access.HEADER: guarded}).status_code == 200


def test_a_wrong_token_is_refused(guarded):
    with phone_client() as client:
        assert client.get("/api/status",
                          headers={access.HEADER: guarded + "x"}).status_code == 401


def test_the_query_token_pairs_the_device_by_cookie(guarded):
    with phone_client() as client:
        r = client.get(f"/?t={guarded}")
        assert r.status_code == 200
        assert access.COOKIE in r.cookies
        # the cookie alone now carries the device
        assert client.get("/api/status",
                          headers={"cookie": f"{access.COOKIE}={guarded}"}).status_code == 200


def test_the_static_assets_are_behind_the_gate_too(guarded):
    """Otherwise the panel's code is readable by anyone who can route to it."""
    with phone_client() as client:
        assert client.get("/assets/app.js").status_code == 401


def test_a_browser_navigation_gets_a_page_not_a_json_blob(guarded):
    with phone_client() as client:
        r = client.get("/", headers={"accept": "text/html"})
        assert r.status_code == 401
        assert "text/html" in r.headers["content-type"]
        assert "access token" in r.text


def test_a_tunnelled_request_loses_the_loopback_exemption(guarded):
    """cloudflared dials in over loopback; the whole internet must not inherit that."""
    with local_client() as client:
        r = client.get("/api/status", headers={"x-forwarded-for": "203.0.113.9"})
        assert r.status_code == 401
        r = client.get("/api/status", headers={"x-forwarded-for": "203.0.113.9",
                                               access.HEADER: guarded})
        assert r.status_code == 200


def test_the_websocket_is_gated(guarded):
    from starlette.websockets import WebSocketDisconnect

    with local_client() as client:
        with pytest.raises((WebSocketDisconnect, Exception)):
            with client.websocket_connect("/ws/market",
                                          headers={"x-forwarded-for": "203.0.113.9"}):
                pass


# ------------------------------------------------------------------ pairing details
def test_pairing_details_are_local_only(guarded):
    with local_client() as client:
        local = client.get("/api/access").json()
        assert local["token"] == guarded and local["url"].endswith(guarded)

    with phone_client() as client:
        away = client.get("/api/access", headers={access.HEADER: guarded}).json()
        assert away["required"] is True
        assert away["token"] is None and away["url"] is None and away["qr"] is None


def test_rotation_endpoint_refuses_remote_callers(guarded):
    with local_client() as client:
        r = client.post("/api/access/rotate", headers={access.HEADER: guarded,
                                                       "x-forwarded-for": "203.0.113.9"})
        assert r.status_code == 403


# ------------------------------------------------------------------------- net utils
def test_wildcard_bind_advertises_a_reachable_address():
    url = net.panel_url("0.0.0.0", 8000, "tok")
    assert url.startswith("http://") and "0.0.0.0" not in url and url.endswith("?t=tok")


def test_loopback_detection():
    assert net.is_loopback_host("127.0.0.1") and net.is_loopback_host("localhost")
    assert not net.is_loopback_host("0.0.0.0") and not net.is_loopback_host("192.168.1.4")


def test_qr_encodes_the_pairing_url():
    svg = net.qr_svg("http://192.168.1.4:8000/?t=abc")
    assert svg is not None and svg.startswith("<svg") and "path" in svg
    assert net.qr_terminal("http://192.168.1.4:8000/?t=abc")
