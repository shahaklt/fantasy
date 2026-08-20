"""Game weather: forecast ahead of kickoff, nflverse behind it.

Two sources, because they answer different halves of the question.

Completed games already carry ``temp`` and ``wind`` in the nflverse schedule,
which is what the effects are fitted on. Games that have not kicked off carry
nulls, so a forecast has to be fetched — Open-Meteo, which needs no API key and
no account.

Indoor games are not fetched at all. A closed roof is a known constant, and
asking a weather service what the wind is doing inside the Superdome would be
both wasteful and wrong.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from ..config import CACHE_DIR
from .stadiums import Stadium, lookup

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_PATH = CACHE_DIR / "weather_forecasts.json"
CACHE_TTL = 3 * 3600          # forecasts move; three hours is plenty fresh
FORECAST_HORIZON_DAYS = 16    # Open-Meteo's limit; beyond it we have no forecast

# What a closed roof actually is: still air at room temperature.
INDOOR = {"temp_f": 70.0, "wind_mph": 0.0, "precip_chance": 0.0,
          "precip_in": 0.0, "humidity": 50.0, "source": "indoor"}


@dataclass
class GameWeather:
    """Conditions for one game. `source` says where the numbers came from."""

    temp_f: float
    wind_mph: float
    precip_chance: float = 0.0      # 0-1
    precip_in: float = 0.0
    humidity: float = 50.0
    elevation_ft: float = 0.0
    indoor: bool = False
    source: str = "unknown"         # indoor | forecast | historical | default

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def known(self) -> bool:
        return self.source in ("indoor", "forecast", "historical")


def _roof_is_closed(roof: str | None) -> bool:
    """nflverse reports the roof state per game, which is the only honest signal.

    A retractable roof is a coin flip decided on the day, so "retractable" in
    the stadium table is not enough — the game row has to say "closed".
    """
    return (roof or "").strip().lower() in ("dome", "closed")


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache))
    except OSError as exc:
        log.debug("could not write weather cache: %s", exc)


def fetch_forecast(stadium: Stadium, kickoff: datetime, timeout: int = 20) -> GameWeather | None:
    """Hourly forecast for one venue at one kickoff, or None if unavailable.

    Returns None rather than raising: a missing forecast should leave a game
    on neutral conditions, not take down a projection build.
    """
    import requests

    now = datetime.now(timezone.utc)
    days_out = (kickoff - now).total_seconds() / 86400.0
    if days_out > FORECAST_HORIZON_DAYS:
        return None                       # honestly beyond forecast range
    if days_out < -1:
        return None                       # in the past; use the historical path

    key = f"{stadium.lat:.3f},{stadium.lon:.3f}@{kickoff.strftime('%Y-%m-%dT%H')}"
    cache = _load_cache()
    hit = cache.get(key)
    if hit and time.time() - hit.get("_fetched", 0) < CACHE_TTL:
        payload = {k: v for k, v in hit.items() if not k.startswith("_")}
        return GameWeather(**payload)

    try:
        resp = requests.get(FORECAST_URL, timeout=timeout, params={
            "latitude": stadium.lat, "longitude": stadium.lon,
            "hourly": "temperature_2m,wind_speed_10m,precipitation,"
                      "precipitation_probability,relative_humidity_2m",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "precipitation_unit": "inch", "timezone": "UTC",
            "forecast_days": max(1, min(FORECAST_HORIZON_DAYS, int(days_out) + 2)),
        })
        resp.raise_for_status()
        hourly = resp.json().get("hourly", {})
    except Exception as exc:  # noqa: BLE001
        log.info("forecast unavailable for %s: %s", stadium.name, exc)
        return None

    times = hourly.get("time") or []
    if not times:
        return None
    target = kickoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00")
    idx = times.index(target) if target in times else min(
        range(len(times)), key=lambda i: abs(
            datetime.fromisoformat(times[i]).replace(tzinfo=timezone.utc) - kickoff))

    def at(name, default=0.0):
        series = hourly.get(name) or []
        value = series[idx] if idx < len(series) else None
        return float(default if value is None else value)

    weather = GameWeather(
        temp_f=at("temperature_2m", 60.0),
        wind_mph=at("wind_speed_10m", 0.0),
        precip_chance=at("precipitation_probability", 0.0) / 100.0,
        precip_in=at("precipitation", 0.0),
        humidity=at("relative_humidity_2m", 50.0),
        elevation_ft=float(stadium.elevation_ft),
        indoor=False, source="forecast")

    cache[key] = {**weather.as_dict(), "_fetched": time.time()}
    _save_cache(cache)
    return weather


def for_game(home_team: str, kickoff: datetime | None = None, *,
             roof: str | None = None, stadium_name: str | None = None,
             temp: float | None = None, wind: float | None = None,
             fetch: bool = True) -> GameWeather:
    """Conditions for one game, from whichever source can answer.

    Order of preference: a closed roof (known), the values nflverse already
    recorded for a completed game, then a forecast, then neutral defaults that
    are flagged as such so nothing downstream mistakes them for a measurement.
    """
    venue = lookup(home_team, stadium_name)
    elevation = float(venue.elevation_ft) if venue else 0.0

    if _roof_is_closed(roof) or (venue and venue.is_indoor and roof is None):
        return GameWeather(**INDOOR, elevation_ft=elevation, indoor=True)

    if temp is not None and wind is not None:
        return GameWeather(temp_f=float(temp), wind_mph=float(wind),
                           elevation_ft=elevation, source="historical")

    if fetch and venue and kickoff is not None:
        forecast = fetch_forecast(venue, kickoff)
        if forecast is not None:
            return forecast

    # Neutral, and labelled: a default must never be mistaken for a reading.
    return GameWeather(temp_f=60.0, wind_mph=6.0, elevation_ft=elevation, source="default")
