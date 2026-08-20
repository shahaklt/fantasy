"""Command line interface.

    gridiron serve                 # start the local web app (this is the main one)
    gridiron refresh               # pull fresh data
    gridiron build --sims 20000    # rebuild projections and simulations
    gridiron validate              # check the model against history
    gridiron calibrate             # refit simulation constants from data
    gridiron doctor                # check the environment, GPU and connectivity
"""
from __future__ import annotations

import json
import sys

import typer
from rich.console import Console
from rich.table import Table

from .config import current_season, detect_backend

app = typer.Typer(add_completion=False, help="Monte Carlo fantasy football and market engine")
console = Console()


def _print_pairing(host: str, port: int, token: str | None, guarded: bool,
                   heading: str = "Open on your phone") -> None:
    """The whole pairing story in one block: where, with what, and how to scan."""
    from . import net

    pair = net.pairing(host, port, token, guarded)
    console.print(f"\n[bold cyan]{heading}[/]  [dim](same wifi as this machine)[/]")
    console.print(f"  [bold]{pair.url}[/]")
    for alt in pair.alternates():
        console.print(f"  [dim]or {alt}[/]")

    drawing = net.qr_terminal(pair.url)
    if drawing:
        console.print("")
        # Printed raw: rich would try to style the block characters.
        print(drawing)
    elif guarded:
        console.print("  [dim]pip install segno to get a scannable QR code here[/]")

    if guarded:
        console.print(f"  access token: [bold]{token}[/]")
        console.print("  [dim]the link carries the token; after the first load the device is "
                      "remembered[/]")
    else:
        console.print("  [yellow]no token required — bound to loopback only[/]")


def _start_tunnel(port: int, token: str | None) -> None:
    """Expose the panel beyond the house via a cloudflared quick tunnel."""
    import re
    import subprocess
    import threading

    from . import net

    cmd = net.tunnel_command()
    if not cmd:
        console.print("[yellow]--tunnel needs cloudflared on PATH[/] "
                      "(brew install cloudflared / winget install Cloudflare.cloudflared)")
        return

    proc = subprocess.Popen([*cmd, f"http://127.0.0.1:{port}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)

    def watch():
        pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
        for line in proc.stdout or ():
            match = pattern.search(line)
            if match:
                url = f"{match.group(0)}/?t={token}" if token else match.group(0)
                console.print("\n[bold cyan]Tunnel up — reachable from anywhere[/]")
                console.print(f"  [bold]{url}[/]")
                drawing = net.qr_terminal(url)
                if drawing:
                    print(drawing)
                console.print("  [dim]this URL is public; only the token stands in front of "
                              "it, and it dies with this process[/]")
                break

    threading.Thread(target=watch, daemon=True).start()

    import atexit

    atexit.register(proc.terminate)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False,
          scheduler: bool = True, demo: bool = typer.Option(False, help="add a synthetic venue"),
          lan: bool = typer.Option(False, "--lan", help="serve to phones on your wifi"),
          tunnel: bool = typer.Option(False, "--tunnel",
                                      help="also expose it off your network via cloudflared"),
          token: bool = typer.Option(None, "--token/--no-token",
                                     help="force the access token on or off")):
    """Start the local web app.

    Plain `serve` is loopback-only and needs no token. `serve --lan` binds every
    interface so a phone on the same wifi can reach it, and puts an access token
    in front of everything that is not this machine.
    """
    import os

    if demo:
        os.environ["GRIDIRON_DEMO_VENUE"] = "1"
    if lan or tunnel:
        host = "0.0.0.0"

    from . import net
    from .api import access
    from .api.server import run

    required = token if token is not None else (tunnel or not net.is_loopback_host(host))
    active_token = access.configure(required=required, host=host, port=port)

    console.print(f"[bold cyan]Gridiron[/] on http://{host}:{port}  ·  {detect_backend().describe()}")
    if required:
        _print_pairing(host, port, active_token, required)
    if tunnel:
        _start_tunnel(port, active_token)

    run(host=host, port=port, reload=reload, start_scheduler=scheduler, require_token=required)


@app.command()
def pair(port: int = 8000, rotate: bool = typer.Option(False, help="issue a new token first")):
    """Reprint the phone pairing link and QR code for a running server."""
    from .api import access

    if rotate:
        try:
            access.rotate_token()
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
        console.print("[yellow]token rotated — previously paired devices must scan again[/]")
    _print_pairing("0.0.0.0", port, access.access_token(), True, heading="Pair a device")


@app.command()
def refresh(force: bool = True):
    """Download the latest data from every source."""
    from . import pipeline

    result = pipeline.refresh_data(force=force)
    table = Table("source", "result")
    for k, v in result["steps"].items():
        table.add_row(k, f"[green]{v}[/]" if v.startswith("ok") else f"[red]{v}[/]")
    console.print(table)
    console.print(f"finished in {result['seconds']}s")


@app.command()
def build(sims: int = 5000, season: int | None = None, refresh_first: bool = False):
    """Rebuild projections and run the season simulation."""
    from . import pipeline

    if refresh_first:
        pipeline.refresh_data(force=True)
    arts = pipeline.build_all(season=season, n_sims=sims)
    console.print_json(json.dumps(arts.meta, default=str))
    top = arts.board.head(15)
    table = Table("player", "pos", "team", "proj", "vorp", "$", "tier")
    for r in top.iter_rows(named=True):
        table.add_row(r["player_name"], r["position"], r["team"],
                      f"{r['proj_points']:.0f}", f"{r.get('vorp', 0):.0f}",
                      f"${r.get('auction_value', 0):.0f}", str(r.get("tier", "")))
    console.print(table)


@app.command()
def validate(sims: int = 2000, season: int | None = None):
    """Compare simulated positional finishes against actual history."""
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "scripts"))
    from validate_totals import main as run_validation  # type: ignore

    run_validation(n_sims=sims, season=season or current_season())


@app.command()
def calibrate(seasons: int = 3, write: bool = True):
    """Refit the simulator's constants from the latest data."""
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "scripts"))
    from calibrate import main as run_calibration  # type: ignore

    run_calibration(lookback=seasons, write=write)


@app.command()
def doctor():
    """Check the environment: compute backend, data sources, venues."""
    import requests

    backend = detect_backend()
    console.print(f"[bold]Compute[/]: {backend.describe()}")

    found = False
    try:
        import cupy

        n = cupy.cuda.runtime.getDeviceCount()
        console.print(f"  cupy {cupy.__version__}, CUDA devices: {n}")
        for i in range(n):
            props = cupy.cuda.runtime.getDeviceProperties(i)
            name = props["name"]
            name = name.decode() if isinstance(name, bytes) else str(name)
            free, total = cupy.cuda.Device(i).mem_info
            console.print(f"    [{i}] {name} — {total / 1024**3:.1f} GB "
                          f"({free / 1024**3:.1f} GB free)")
        found = n > 0
    except ImportError:
        console.print("  [yellow]cupy not installed[/]  ->  pip install cupy-cuda12x")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]cupy present but no usable device: {exc}[/]")

    try:
        import torch

        console.print(f"  torch {torch.__version__}, cuda available: {torch.cuda.is_available()}")
        found = found or torch.cuda.is_available()
    except ImportError:
        console.print("  torch not installed (optional fallback backend)")

    if not found:
        console.print("  [yellow]running on CPU — simulations will be slower[/]")

    table = Table("check", "status")
    checks = [
        ("nflverse schedules", "https://github.com/nflverse/nflverse-data/releases/download/players/players.parquet"),
        ("FantasyPros ECR", "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_fpecr_latest.csv"),
        ("Kalshi", "https://api.elections.kalshi.com/trade-api/v2/exchange/status"),
        ("Polymarket", "https://gamma-api.polymarket.com/markets?limit=1"),
    ]
    for name, url in checks:
        try:
            r = requests.head(url, timeout=12, allow_redirects=True)
            ok = r.status_code < 400
            if not ok:
                r = requests.get(url, timeout=12, stream=True)
                ok = r.status_code < 400
            table.add_row(name, f"[green]reachable ({r.status_code})[/]" if ok
                          else f"[yellow]http {r.status_code}[/]")
        except Exception as exc:  # noqa: BLE001
            table.add_row(name, f"[red]{str(exc)[:60]}[/]")
    console.print(table)

    from .exchange.kalshi import KalshiClient
    from .exchange.polymarket import PolymarketClient
    from .exchange.risk import RiskGuard

    console.print(f"Kalshi credentials: {'configured' if KalshiClient().authenticated else 'not configured'}")
    console.print(f"Polymarket credentials: {'configured' if PolymarketClient().authenticated else 'not configured'}")
    guard = RiskGuard()
    console.print(f"Trading mode: [bold]{guard.mode.value}[/]"
                  + ("  [red](kill switch active)[/]" if guard.kill_switch_active() else ""))


@app.command()
def board(position: str = "ALL", limit: int = 30):
    """Print the current draft board in the terminal."""
    from . import pipeline

    arts = pipeline.get_artifacts()
    b = arts.board
    if position.upper() != "ALL":
        b = b.filter(__import__("polars").col("position") == position.upper())
    table = Table("#", "player", "pos", "tm", "proj", "vorp", "$", "tier", "adp")
    for i, r in enumerate(b.head(limit).iter_rows(named=True), 1):
        table.add_row(str(i), r["player_name"], r["position"], r["team"],
                      f"{r['proj_points']:.0f}", f"{r.get('vorp', 0):.0f}",
                      f"${r.get('auction_value', 0):.0f}", str(r.get("tier", "")),
                      f"{r.get('adp') or 0:.1f}")
    console.print(table)


if __name__ == "__main__":
    app()
