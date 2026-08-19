"""Matching quoted contracts to the right model probability.

A market title carries three things that matter: which teams it concerns, what
*kind* of bet it is, and (usually) a line. Matching on teams alone is how you
end up pricing a totals contract off a moneyline probability and betting real
money on a number that answers a different question, so the kind and the line
are parsed explicitly and a market that cannot be classified is skipped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

MONEYLINE_WORDS = ("beat", "beats", "win", "wins", "defeat", "defeats", "victory",
                   "winner", "moneyline", "to win")
TOTAL_WORDS = ("total", "totals", "over", "under", "o/u", "combined points", "points scored")
SPREAD_WORDS = ("spread", "cover", "covers", "handicap", "margin", "by more than")

#: Signed line, so "cover -3.5" keeps its sign. The negative lookbehind stops a
#: date or a year inside a ticker from being read as a line.
LINE_RE = re.compile(r"(?<![\w.])(-?\d{1,3}(?:\.\d)?)(?![\w])")


def _has_word(text: str, words) -> bool:
    """Whole-word match.

    Substring matching is a trap here: "cover" contains "over", so a spread
    contract classified by substring becomes a totals contract and gets priced
    against the wrong distribution.
    """
    return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text) for w in words)


def classify_market(title: str, ticker: str = "") -> tuple[str, float | None]:
    """Return ``(kind, line)`` for a contract title.

    kind is one of ``moneyline``, ``total``, ``spread``, ``team_total`` or
    ``unknown``. Spread is tested before total because spread wording ("cover
    -3.5") frequently also contains a number and a directional word.
    """
    text = f"{title} {ticker}".lower()
    line = None
    m = LINE_RE.search(title or "")
    if m:
        try:
            line = float(m.group(1))
        except ValueError:
            line = None

    if _has_word(text, SPREAD_WORDS):
        return "spread", line
    if _has_word(text, TOTAL_WORDS):
        # "Chiefs team total over 24.5" is a team total, not a game total.
        kind = "team_total" if ("team total" in text or "team points" in text) else "total"
        return kind, line
    if _has_word(text, MONEYLINE_WORDS):
        return "moneyline", None
    return "unknown", line


@dataclass
class GameProbabilities:
    """The simulated distribution of one game, queryable at any line."""

    home_team: str
    away_team: str
    home_points: np.ndarray = field(repr=False)
    away_points: np.ndarray = field(repr=False)
    game_id: str = ""

    @property
    def margin(self) -> np.ndarray:
        return self.home_points - self.away_points

    @property
    def total(self) -> np.ndarray:
        return self.home_points + self.away_points

    def moneyline(self, team: str) -> float:
        m = self.margin
        p = float((m > 0).mean() + 0.5 * (m == 0).mean())
        return p if team == self.home_team else 1.0 - p

    def over(self, line: float) -> float:
        return float((self.total > line).mean())

    def cover(self, team: str, line: float) -> float:
        """P(team wins by more than `line` points)."""
        m = self.margin if team == self.home_team else -self.margin
        return float((m > line).mean())

    def team_total_over(self, team: str, line: float) -> float:
        pts = self.home_points if team == self.home_team else self.away_points
        return float((pts > line).mean())

    def sample_size(self) -> int:
        return int(len(self.home_points))


class ProbabilityBook:
    """All simulated games, indexed so a market title can be priced."""

    def __init__(self):
        self.games: list[GameProbabilities] = []

    def add(self, game: GameProbabilities) -> None:
        self.games.append(game)

    @classmethod
    def from_predictions(cls, predictions) -> "ProbabilityBook":
        book = cls()
        for p in predictions:
            book.add(GameProbabilities(
                home_team=p.home_team, away_team=p.away_team,
                home_points=p.home_points, away_points=p.away_points,
                game_id=p.game_id))
        return book

    def find_game(self, teams: list[str]) -> GameProbabilities | None:
        if not teams:
            return None
        s = set(teams)
        # Prefer a game that names both teams; fall back to one team appearing.
        for g in self.games:
            if {g.home_team, g.away_team} <= s or s <= {g.home_team, g.away_team}:
                return g
        for g in self.games:
            if g.home_team in s or g.away_team in s:
                return g
        return None

    def price(self, title: str, ticker: str, teams: list[str]) -> tuple[float, str, str] | None:
        """Return ``(probability, description, game_id)`` for a contract.

        Returns None when the market cannot be matched confidently -- an
        unmatched market is skipped, never guessed at.
        """
        game = self.find_game(teams)
        if game is None:
            return None
        kind, line = classify_market(title, ticker)
        # `teams` arrives ordered by position in the title, so the first entry is
        # the side the contract is about ("Eagles cover -3.5 vs Cowboys" -> PHI).
        subject = teams[0] if teams else game.home_team

        if kind == "moneyline":
            return game.moneyline(subject), f"{subject} moneyline", game.game_id
        if kind == "total" and line is not None:
            return game.over(line), f"game total over {line}", game.game_id
        if kind == "team_total" and line is not None:
            return game.team_total_over(subject, line), f"{subject} team total over {line}", game.game_id
        if kind == "spread" and line is not None:
            return game.cover(subject, line), f"{subject} covers {line}", game.game_id
        return None
