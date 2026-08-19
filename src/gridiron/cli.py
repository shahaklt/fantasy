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


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False,
          scheduler: bool = True, demo: bool = typer.Option(False, help="add a synthetic venue")):
    """Start the local web app."""
    import os

    if demo:
        os.environ["GRIDIRON_DEMO_VENUE"] = "1"
    from .api.server import run

    console.print(f"[bold cyan]Gridiron[/] on http://{host}:{port}  ·  {detect_backend().describe()}")
    run(host=host, port=port, reload=reload, start_scheduler=scheduler)


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
    try:
        import torch

        console.print(f"  torch {torch.__version__}, cuda available: {torch.cuda.is_available()}")
    except ImportError:
        console.print("  [yellow]torch not installed — GPU acceleration unavailable[/]")
        console.print("  install with: pip install torch --index-url https://download.pytorch.org/whl/cu121")

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
