"""Venue metadata: where each stadium is, how high, and what it is made of.

nflverse carries roof and surface per game, but not coordinates or elevation,
and those are the two things you cannot derive from a box score. Coordinates
are what a weather forecast is keyed on; elevation changes air density, which
changes how far a kicked ball travels.

Elevations are field level in feet, rounded — the difference between 8 ft and
12 ft above sea level does not move a football, and pretending to more
precision than that would be false confidence. Denver is the only venue where
altitude is large enough to matter on its own.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Stadium:
    team: str                 # current primary tenant, nflverse abbreviation
    name: str
    lat: float
    lon: float
    elevation_ft: int
    roof: str                 # "outdoors" | "dome" | "retractable"
    surface: str              # "grass" | "turf"
    tz: str

    @property
    def is_indoor(self) -> bool:
        """Retractable roofs are treated as outdoor until a game says otherwise.

        nflverse reports the roof state per game ("closed" vs "open"), which is
        the only reliable signal — teams decide on the day.
        """
        return self.roof == "dome"


# Shared venues get one entry per tenant so a team code always resolves.
STADIUMS: tuple[Stadium, ...] = (
    Stadium("ARI", "State Farm Stadium",        33.5276, -112.2626, 1070, "retractable", "grass", "America/Phoenix"),
    Stadium("ATL", "Mercedes-Benz Stadium",     33.7554,  -84.4008, 1050, "retractable", "turf",  "America/New_York"),
    Stadium("BAL", "M&T Bank Stadium",          39.2780,  -76.6227,   30, "outdoors",    "grass", "America/New_York"),
    Stadium("BUF", "Highmark Stadium",          42.7738,  -78.7870,  600, "outdoors",    "turf",  "America/New_York"),
    Stadium("CAR", "Bank of America Stadium",   35.2258,  -80.8528,  730, "outdoors",    "turf",  "America/New_York"),
    Stadium("CHI", "Soldier Field",             41.8623,  -87.6167,  600, "outdoors",    "grass", "America/Chicago"),
    Stadium("CIN", "Paycor Stadium",            39.0955,  -84.5161,  490, "outdoors",    "turf",  "America/New_York"),
    Stadium("CLE", "Huntington Bank Field",     41.5061,  -81.6995,  570, "outdoors",    "grass", "America/New_York"),
    Stadium("DAL", "AT&T Stadium",              32.7473,  -97.0945,  600, "retractable", "turf",  "America/Chicago"),
    Stadium("DEN", "Empower Field at Mile High",39.7439, -105.0201, 5280, "outdoors",    "grass", "America/Denver"),
    Stadium("DET", "Ford Field",                42.3400,  -83.0456,  600, "dome",        "turf",  "America/Detroit"),
    Stadium("GB",  "Lambeau Field",             44.5013,  -88.0622,  640, "outdoors",    "grass", "America/Chicago"),
    Stadium("HOU", "NRG Stadium",               29.6847,  -95.4107,   50, "retractable", "turf",  "America/Chicago"),
    Stadium("IND", "Lucas Oil Stadium",         39.7601,  -86.1639,  715, "retractable", "turf",  "America/Indiana/Indianapolis"),
    Stadium("JAX", "EverBank Stadium",          30.3239,  -81.6373,   15, "outdoors",    "grass", "America/New_York"),
    Stadium("KC",  "GEHA Field at Arrowhead",   39.0489,  -94.4839,  750, "outdoors",    "grass", "America/Chicago"),
    Stadium("LA",  "SoFi Stadium",              33.9535, -118.3392,  100, "dome",        "turf",  "America/Los_Angeles"),
    Stadium("LAC", "SoFi Stadium",              33.9535, -118.3392,  100, "dome",        "turf",  "America/Los_Angeles"),
    Stadium("LV",  "Allegiant Stadium",         36.0909, -115.1833, 2030, "dome",        "grass", "America/Los_Angeles"),
    Stadium("MIA", "Hard Rock Stadium",         25.9580,  -80.2389,   10, "outdoors",    "grass", "America/New_York"),
    Stadium("MIN", "U.S. Bank Stadium",         44.9736,  -93.2575,  830, "dome",        "turf",  "America/Chicago"),
    Stadium("NE",  "Gillette Stadium",          42.0909,  -71.2643,  290, "outdoors",    "turf",  "America/New_York"),
    Stadium("NO",  "Caesars Superdome",         29.9511,  -90.0812,    3, "dome",        "turf",  "America/Chicago"),
    Stadium("NYG", "MetLife Stadium",           40.8135,  -74.0745,   10, "outdoors",    "turf",  "America/New_York"),
    Stadium("NYJ", "MetLife Stadium",           40.8135,  -74.0745,   10, "outdoors",    "turf",  "America/New_York"),
    Stadium("PHI", "Lincoln Financial Field",   39.9008,  -75.1675,   40, "outdoors",    "grass", "America/New_York"),
    Stadium("PIT", "Acrisure Stadium",          40.4468,  -80.0158,  730, "outdoors",    "grass", "America/New_York"),
    Stadium("SEA", "Lumen Field",               47.5952, -122.3316,   15, "outdoors",    "turf",  "America/Los_Angeles"),
    Stadium("SF",  "Levi's Stadium",            37.4033, -121.9694,   10, "outdoors",    "grass", "America/Los_Angeles"),
    Stadium("TB",  "Raymond James Stadium",     27.9759,  -82.5033,   25, "outdoors",    "grass", "America/New_York"),
    Stadium("TEN", "Nissan Stadium",            36.1665,  -86.7713,  440, "outdoors",    "grass", "America/Chicago"),
    Stadium("WAS", "Northwest Stadium",         38.9076,  -76.8645,  200, "outdoors",    "grass", "America/New_York"),
)

BY_TEAM: dict[str, Stadium] = {s.team: s for s in STADIUMS}

# International games. The host team still "owns" the game in nflverse, so these
# are looked up by the schedule's own stadium name rather than by team.
NEUTRAL_SITES: dict[str, Stadium] = {
    "Estadio Azteca":      Stadium("",  "Estadio Azteca",      19.3029, -99.1505, 7280, "outdoors", "grass", "America/Mexico_City"),
    "Tottenham Hotspur Stadium": Stadium("", "Tottenham Hotspur Stadium", 51.6043, -0.0665, 100, "outdoors", "grass", "Europe/London"),
    "Wembley Stadium":     Stadium("",  "Wembley Stadium",     51.5560,  -0.2795,  100, "outdoors", "grass", "Europe/London"),
    "Allianz Arena":       Stadium("",  "Allianz Arena",       48.2188,  11.6247, 1700, "outdoors", "grass", "Europe/Berlin"),
    "Deutsche Bank Park":  Stadium("",  "Deutsche Bank Park",  50.0686,   8.6455,  350, "outdoors", "grass", "Europe/Berlin"),
    "Neo Quimica Arena":   Stadium("",  "Neo Quimica Arena",  -23.5453, -46.4742, 2560, "outdoors", "grass", "America/Sao_Paulo"),
    "Santiago Bernabeu":   Stadium("",  "Santiago Bernabeu",   40.4531,  -3.6883, 2200, "outdoors", "grass", "Europe/Madrid"),
    "Croke Park":          Stadium("",  "Croke Park",          53.3607,  -6.2512,   65, "outdoors", "grass", "Europe/Dublin"),
}

# Sea-level reference for the air-density ratio. Kicking distance scales with
# density, and density falls roughly exponentially with a ~27,000 ft scale height.
SCALE_HEIGHT_FT = 27_000.0


def lookup(team: str | None = None, stadium_name: str | None = None) -> Stadium | None:
    """Venue for a game, preferring an explicit stadium name over the home team."""
    if stadium_name:
        for name, s in NEUTRAL_SITES.items():
            if name.lower() in stadium_name.lower():
                return s
    if team:
        return BY_TEAM.get(team.upper())
    return None


def air_density_ratio(elevation_ft: float) -> float:
    """Air density relative to sea level.

    A kicked ball loses less energy to drag in thin air, which is the whole of
    the Denver effect and the reason it applies indoors at Allegiant too — a
    roof keeps the wind out, not the altitude.
    """
    import math

    return math.exp(-max(elevation_ft, 0.0) / SCALE_HEIGHT_FT)
