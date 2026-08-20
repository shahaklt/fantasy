"""Token gate for serving the panel to something other than this machine.

The panel is not a read-only dashboard. It holds league credentials and, if you
configure them, exchange credentials, and it exposes endpoints that place
orders. On loopback that is fine — the only client is you. The moment it binds
to the wifi it is reachable by every device on the network, including whatever
else is on the guest VLAN, so from that point a token is required.

The rules, in one place:

  · loopback clients are always allowed, whatever the bind address
  · every other client needs the token, on every path, including the websocket
  · the token can arrive as ?t=, an X-Gridiron-Token header, a Bearer header,
    or the cookie the first two set, so pairing is a single scan
  · the token itself is only ever disclosed to a loopback client
"""
from __future__ import annotations

import ipaddress
import os
import secrets
from dataclasses import dataclass

from starlette.datastructures import Headers
from starlette.responses import HTMLResponse, JSONResponse

from ..config import USER_DIR

TOKEN_PATH = USER_DIR / "access_token"
COOKIE = "gridiron_access"
HEADER = "x-gridiron-token"
QUERY = "t"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365


# --------------------------------------------------------------------------- token
def _generate() -> str:
    return secrets.token_urlsafe(16)


def access_token(create: bool = True) -> str | None:
    """The shared secret for remote clients.

    An explicit GRIDIRON_TOKEN wins so the value can be pinned by whoever runs
    the process; otherwise it is generated once and kept in the user directory
    with the same 0600 treatment as the league cookies.
    """
    env = (os.environ.get("GRIDIRON_TOKEN") or "").strip()
    if env:
        return env
    if TOKEN_PATH.exists():
        stored = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    if not create:
        return None
    token = _generate()
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token + "\n", encoding="utf-8")
    try:
        TOKEN_PATH.chmod(0o600)
    except OSError:                                   # Windows, and that is fine
        pass
    return token


def rotate_token() -> str:
    """Invalidate every paired device. Used by the Settings panel."""
    if os.environ.get("GRIDIRON_TOKEN"):
        raise RuntimeError(
            "the token is pinned by the GRIDIRON_TOKEN environment variable; "
            "change it there and restart instead")
    TOKEN_PATH.unlink(missing_ok=True)
    return access_token()


# -------------------------------------------------------------------------- config
@dataclass
class AccessSettings:
    required: bool = False
    host: str = "127.0.0.1"
    port: int = 8000


def _settings_from_env() -> AccessSettings:
    """Reload workers are fresh processes, so the bind details travel by env."""
    return AccessSettings(
        required=(os.environ.get("GRIDIRON_REQUIRE_TOKEN", "") or "").strip().lower()
        in ("1", "true", "yes", "on"),
        host=os.environ.get("GRIDIRON_BIND_HOST", "127.0.0.1"),
        port=int(os.environ.get("GRIDIRON_BIND_PORT", "8000") or 8000),
    )


settings = _settings_from_env()


def is_loopback_client(scope) -> bool:
    """Whether this request came from the machine itself, and can be trusted.

    A tunnel (cloudflared, ngrok) dials the server over loopback, so the peer
    address alone would wave the whole internet through. Those hops always add
    a forwarding header, and nothing on this machine has a reason to send one,
    so its presence disqualifies the request from the local exemption.
    """
    headers = Headers(scope=scope)
    if headers.get("x-forwarded-for") or headers.get("forwarded") or headers.get("cf-connecting-ip"):
        return False
    client = scope.get("client")
    if not client:
        # No peer address means an in-process transport (the test client), which
        # is as local as it gets.
        return True
    try:
        return ipaddress.ip_address(str(client[0])).is_loopback
    except ValueError:
        return False


def _presented(scope) -> tuple[str | None, bool]:
    """The token this request carries, and whether it came from the URL."""
    headers = Headers(scope=scope)
    raw = headers.get(HEADER)
    if raw:
        return raw.strip(), False
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip(), False

    query = scope.get("query_string", b"").decode("latin-1")
    if query:
        from urllib.parse import parse_qs

        values = parse_qs(query).get(QUERY)
        if values and values[0].strip():
            return values[0].strip(), True

    cookies = headers.get("cookie", "")
    for part in cookies.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE and value:
            return value.strip(), False
    return None, False


PAIR_PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gridiron — pair this device</title>
<style>
 html,body{margin:0;height:100%%;background:#05080c;color:#dce6f2;
   font:14px/1.6 ui-monospace,Menlo,Consolas,monospace;
   display:grid;place-items:center;padding:24px}
 form{width:min(360px,100%%);border:1px solid #1c2735;background:#0a0f16}
 h1{margin:0;padding:12px 14px;font:600 12px/1 ui-monospace,monospace;letter-spacing:.1em;
   text-transform:uppercase;color:#9fb2c8;background:#0f151e;border-bottom:1px solid #1c2735}
 div{padding:14px;display:grid;gap:10px}
 p{margin:0;color:#7489a1;font-size:12px}
 input{width:100%%;padding:12px;font:16px/1.2 ui-monospace,monospace;background:#05080c;
   color:#dce6f2;border:1px solid #2b3a4d;border-radius:0}
 button{padding:12px;font:600 13px/1 ui-monospace,monospace;background:#3ddbd9;color:#05080c;
   border:0;border-radius:0;cursor:pointer;min-height:44px}
</style>
<form method="get" action="/">
 <h1>Gridiron — locked</h1>
 <div>
  <p>%(message)s</p>
  <input name="t" placeholder="access token" autocomplete="off"
    autocapitalize="off" autocorrect="off" spellcheck="false" />
  <button type="submit">Unlock</button>
  <p>The token is printed in the terminal that started the server, and shown
     under Settings &rsaquo; Phone access on the machine itself.</p>
 </div>
</form>
"""


def _denied(scope, message: str):
    accept = Headers(scope=scope).get("accept", "")
    wants_page = "text/html" in accept and scope.get("method", "GET") == "GET"
    if wants_page:
        return HTMLResponse(PAIR_PAGE % {"message": message}, status_code=401)
    return JSONResponse({"detail": message}, status_code=401)


class AccessGuard:
    """Pure-ASGI so the market websocket is covered by the same rule as /api."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not settings.required:
            return await self.app(scope, receive, send)
        if is_loopback_client(scope):
            return await self.app(scope, receive, send)

        expected = access_token()
        presented, from_url = _presented(scope)
        ok = bool(expected) and bool(presented) and secrets.compare_digest(presented, expected)

        if not ok:
            if scope["type"] == "websocket":
                await receive()
                return await send({"type": "websocket.close", "code": 1008})
            message = ("This device is not paired yet. Scan the QR code in the terminal, "
                       "or enter the access token." if not presented
                       else "That token does not match. Check the terminal for the current one.")
            return await _denied(scope, message)(scope, receive, send)

        if from_url and scope["type"] == "http":
            # Remember the device so the token never has to be typed twice and
            # never lingers in a shared link's history beyond the first load.
            cookie = (f"{COOKIE}={presented}; Path=/; Max-Age={COOKIE_MAX_AGE}; "
                      "SameSite=Lax; HttpOnly").encode("latin-1")

            async def send_with_cookie(message):
                if message["type"] == "http.response.start":
                    message = dict(message)
                    message["headers"] = list(message.get("headers", [])) + [(b"set-cookie", cookie)]
                await send(message)

            return await self.app(scope, receive, send_with_cookie)

        return await self.app(scope, receive, send)


def configure(required: bool, host: str, port: int) -> str | None:
    """Called by run() once the bind address is known."""
    settings.required = required
    settings.host = host
    settings.port = port
    os.environ["GRIDIRON_REQUIRE_TOKEN"] = "1" if required else "0"
    os.environ["GRIDIRON_BIND_HOST"] = host
    os.environ["GRIDIRON_BIND_PORT"] = str(port)
    return access_token() if required else access_token(create=False)
