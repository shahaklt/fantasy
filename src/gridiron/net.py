"""Finding the machine on the local network, and pairing a phone with it.

The panel is a local server. Nothing here opens a hole in anything: it only
answers "which address on this wifi points back at me", and turns that address
into something you can scan instead of typing a token by hand.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}
ANY_HOSTS = {"0.0.0.0", "::", ""}


def is_loopback_host(host: str) -> bool:
    return (host or "").strip().lower() in LOOPBACK_HOSTS


def is_wildcard_host(host: str) -> bool:
    return (host or "").strip() in ANY_HOSTS


def _usable(addr: str) -> bool:
    """Addresses a phone on the same wifi could actually reach."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified)


def _route_probe() -> str | None:
    """Ask the routing table which local address leaves the machine.

    Connecting a UDP socket sends nothing — it only picks a source address —
    so this works with the network unplugged as well as behind a firewall.
    """
    for probe in ("8.8.8.8", "192.168.1.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 53))
            addr = s.getsockname()[0]
            if _usable(addr):
                return addr
        except OSError:
            continue
        finally:
            s.close()
    return None


def lan_addresses() -> list[str]:
    """Every IPv4 address of this machine a phone could plausibly hit.

    Ordered with the routed address first, since on a laptop with docker or a
    VPN up there are usually several and only one of them is the wifi.
    """
    found: list[str] = []
    routed = _route_probe()
    if routed:
        found.append(routed)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if _usable(addr) and addr not in found:
                found.append(addr)
    except OSError:
        pass
    return found


def resolve_host(host: str) -> str:
    """The address to advertise for a server bound to `host`."""
    if is_wildcard_host(host):
        return (lan_addresses() or ["127.0.0.1"])[0]
    return host


def panel_url(host: str, port: int, token: str | None = None) -> str:
    addr = resolve_host(host)
    if ":" in addr and not addr.startswith("["):      # bare IPv6
        addr = f"[{addr}]"
    url = f"http://{addr}:{port}/"
    return f"{url}?t={token}" if token else url


# ------------------------------------------------------------------ QR pairing
def qr_terminal(text: str) -> str | None:
    """A QR code drawn in block characters, or None if segno is unavailable.

    Typing a 22-character token into a phone keyboard is exactly the kind of
    friction that stops a tool from being used, so the terminal offers the
    camera an alternative.
    """
    try:
        import io

        import segno
    except ImportError:
        return None
    qr = segno.make(text, error="m")
    for compact in (True, False):
        buf = io.StringIO()
        try:
            qr.terminal(buf, compact=compact)
            drawing = buf.getvalue()
            # Consoles with a legacy code page raise on the half-block glyphs
            # only at write time, so probe the encoding before returning.
            drawing.encode(getattr(__import__("sys").stdout, "encoding", None) or "utf-8")
            return drawing
        except (UnicodeEncodeError, LookupError):
            continue
    return None


def qr_svg(text: str, scale: int = 4, dark: str = "#05080c", light: str = "#dce6f2") -> str | None:
    """The same code as an inline SVG, for the Settings panel.

    Dark modules on a light field, even inside a dark interface: the standard
    assumes that polarity, and while most phone cameras cope with an inverted
    code, "most" is the wrong tolerance for the one thing that has to work on
    the first try.
    """
    try:
        import io

        import segno
    except ImportError:
        return None
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=scale, border=2,
                                     dark=dark, light=light, xmldecl=False, svgns=True)
    svg = buf.getvalue().decode()

    # segno emits width/height but no viewBox, and without one the browser
    # cannot work out the aspect ratio — the code stretches sideways and stops
    # being square, which is the one property a QR code has to keep.
    import re

    match = re.search(r'width="(\d+)" height="(\d+)"', svg)
    if match:
        svg = svg.replace(match.group(0),
                          f'{match.group(0)} viewBox="0 0 {match.group(1)} {match.group(2)}"', 1)
    return svg


@dataclass
class Pairing:
    """Everything needed to get the panel open on a phone."""
    url: str
    addresses: list[str]
    port: int
    token: str | None
    guarded: bool

    def alternates(self) -> list[str]:
        out = []
        for addr in self.addresses:
            u = f"http://{addr}:{self.port}/"
            if self.token:
                u += f"?t={self.token}"
            if u != self.url:
                out.append(u)
        return out


def pairing(host: str, port: int, token: str | None, guarded: bool) -> Pairing:
    addresses = lan_addresses() if is_wildcard_host(host) else [resolve_host(host)]
    return Pairing(url=panel_url(host, port, token), addresses=addresses,
                   port=port, token=token, guarded=guarded)


def tunnel_command() -> list[str] | None:
    """cloudflared's quick-tunnel invocation, if the binary is installed."""
    import shutil

    exe = shutil.which("cloudflared")
    return [exe, "tunnel", "--url"] if exe else None


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
