"""Monte Carlo draft simulator and best-pick recommender.

A draft board ranked by value tells you who is good. It does not tell you what
to *do*, because the right pick depends on who will still be there when your
next turn comes around. This module answers that directly: for each candidate,
simulate the rest of the draft many times and keep the candidate whose finished
roster scores highest.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import polars as pl

from ..scoring import SLOT_ELIGIBILITY, LeagueSettings
from .availability import linear_picks, snake_picks


@dataclass
class DraftState:
    """Live state of a draft in progress."""

    league: LeagueSettings
    drafted: list[str] = field(default_factory=list)          # player_id, in pick order
    rosters: dict[int, list[str]] = field(default_factory=dict)  # team slot -> player_ids
    my_slot: int = 1

    @property
    def pick_number(self) -> int:
        return len(self.drafted) + 1

    @property
    def rounds(self) -> int:
        return self.league.roster_size

    def team_on_clock(self, pick: int | None = None) -> int:
        pick = pick or self.pick_number
        rnd = (pick - 1) // self.league.teams
        idx = (pick - 1) % self.league.teams
        if self.league.draft_type == "snake" and rnd % 2 == 1:
            idx = self.league.teams - 1 - idx
        return idx + 1

    def my_picks(self) -> list[int]:
        fn = snake_picks if self.league.draft_type == "snake" else linear_picks
        return fn(self.my_slot, self.league.teams, self.rounds)

    def my_next_picks(self, count: int = 4) -> list[int]:
        return [p for p in self.my_picks() if p >= self.pick_number][:count]

    def roster(self, slot: int) -> list[str]:
        return self.rosters.setdefault(slot, [])

    def record(self, player_id: str, slot: int | None = None) -> None:
        slot = slot or self.team_on_clock()
        self.drafted.append(player_id)
        self.roster(slot).append(player_id)

    def undo(self) -> str | None:
        if not self.drafted:
            return None
        pid = self.drafted.pop()
        for ids in self.rosters.values():
            if ids and ids[-1] == pid:
                ids.pop()
                break
        return pid


def optimal_lineup_points(points: np.ndarray, positions: np.ndarray,
                          slots: dict[str, int]) -> float:
    """Best legal starting lineup from a set of players (greedy by scarcity).

    Slots are filled most-restrictive first (single-position before flex), which
    is optimal for the nested eligibility structure fantasy lineups actually use.
    """
    order = sorted(slots.items(), key=lambda kv: len(SLOT_ELIGIBILITY.get(kv[0], ())))
    used = np.zeros(len(points), dtype=bool)
    total = 0.0
    for slot, count in order:
        if slot in ("BN", "IR") or count <= 0:
            continue
        eligible_pos = SLOT_ELIGIBILITY.get(slot, ())
        for _ in range(count):
            mask = (~used) & np.isin(positions, eligible_pos)
            if not mask.any():
                break
            idx = int(np.argmax(np.where(mask, points, -np.inf)))
            used[idx] = True
            total += float(points[idx])
    return total


class DraftSimulator:
    """Simulates opponents and evaluates candidate picks.

    Opponents are modelled as ADP-followers with two realistic distortions:
    a random reach/fall on every pick (Gumbel noise over the consensus board)
    and a roster-need multiplier that makes a team with no quarterback far more
    likely to take one.
    """

    def __init__(self, board: pl.DataFrame, league: LeagueSettings,
                 value_col: str = "proj_points", seed: int | None = None):
        self.league = league
        self.value_col = value_col
        self.rng = np.random.default_rng(seed)

        b = board.filter(pl.col("player_id").is_not_null())
        self.ids = b["player_id"].to_list()
        self.names = b["player_name"].to_list()
        self.pos = b["position"].to_numpy().astype(str)
        self.points = np.nan_to_num(b[value_col].to_numpy(), nan=0.0)
        self.index = {pid: i for i, pid in enumerate(self.ids)}

        adp = b["adp"].to_numpy() if "adp" in b.columns else None
        if adp is None or not np.isfinite(adp).any():
            adp = np.argsort(np.argsort(-self.points)).astype(float) + 1.0
        self.adp = np.nan_to_num(adp, nan=float(len(self.ids)))
        sd = b["adp_sd"].to_numpy() if "adp_sd" in b.columns else np.full(len(self.ids), 12.0)
        self.adp_sd = np.clip(np.nan_to_num(sd, nan=12.0), 2.0, 60.0)

        self.starter_need = league.positional_demand()
        self._pos_limits = self._roster_limits()

    def _roster_limits(self) -> dict[str, int]:
        """Rough cap on how many of each position a sane opponent drafts."""
        r = self.league.roster
        bench = self.league.bench_size
        return {
            "QB": r.get("QB", 1) + (2 if r.get("SUPERFLEX", 0) or r.get("OP", 0) else 1),
            "RB": r.get("RB", 2) + max(2, bench // 2),
            "WR": r.get("WR", 3) + max(2, bench // 2),
            "TE": r.get("TE", 1) + 1,
            "K": r.get("K", 1),
            "DST": r.get("DST", 1),
        }

    # ------------------------------------------------------------------ core
    def _need_multiplier(self, roster_pos: dict[str, int], picks_left: int) -> dict[str, float]:
        """How much an opponent's roster holes distort his board."""
        mult = {}
        for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
            have = roster_pos.get(pos, 0)
            limit = self._pos_limits.get(pos, 99)
            required = self.league.roster.get(pos, 0)
            if have >= limit:
                mult[pos] = 0.02
            elif pos in ("K", "DST"):
                # Nobody takes a kicker early and everybody takes one at the end.
                mult[pos] = 4.0 if picks_left <= 2 else 0.02
            elif have < required:
                mult[pos] = 1.6
            else:
                mult[pos] = 1.0
        return mult

    def simulate_remaining(self, taken: np.ndarray, state: DraftState,
                           my_extra: list[int] | None = None,
                           max_picks: int | None = None) -> np.ndarray:
        """Run the rest of the draft once; return my final roster indices.

        `taken` is a boolean mask over the board that this call mutates locally.
        """
        league = self.league
        taken = taken.copy()
        total_picks = league.total_picks
        pick = state.pick_number + (len(my_extra) if my_extra else 0)
        my_roster = list(my_extra or [])
        for pid in state.roster(state.my_slot):
            i = self.index.get(pid)
            if i is not None and i not in my_roster:
                my_roster.append(i)

        roster_pos: dict[int, dict[str, int]] = {}
        for slot, pids in state.rosters.items():
            counts: dict[str, int] = {}
            for pid in pids:
                i = self.index.get(pid)
                if i is not None:
                    counts[self.pos[i]] = counts.get(self.pos[i], 0) + 1
            roster_pos[slot] = counts
        mine = roster_pos.setdefault(state.my_slot, {})
        for i in (my_extra or []):
            mine[self.pos[i]] = mine.get(self.pos[i], 0) + 1

        limit = max_picks if max_picks is not None else total_picks
        while pick <= min(total_picks, limit):
            slot = state.team_on_clock(pick)
            counts = roster_pos.setdefault(slot, {})
            picks_left = max(league.roster_size - sum(counts.values()), 0)

            if slot == state.my_slot:
                # My own future picks: take the highest projected player that
                # still fits a starting slot -- a deliberately simple policy, so
                # the comparison between candidate picks is not contaminated by
                # a clever-but-arbitrary future strategy.
                cand = self._best_for_me(taken, counts)
                if cand is None:
                    break
                my_roster.append(cand)
                counts[self.pos[cand]] = counts.get(self.pos[cand], 0) + 1
                taken[cand] = True
            else:
                cand = self._opponent_pick(taken, counts, picks_left, pick)
                if cand is None:
                    break
                counts[self.pos[cand]] = counts.get(self.pos[cand], 0) + 1
                taken[cand] = True
            pick += 1
        return np.asarray(my_roster, dtype=np.int64)

    def _opponent_pick(self, taken: np.ndarray, counts: dict[str, int],
                       picks_left: int, pick: int) -> int | None:
        """Pick by noisy ADP, weighted by roster need."""
        avail = np.flatnonzero(~taken)
        if len(avail) == 0:
            return None
        # Only the next slice of the board is realistically in play.
        window = avail[np.argsort(self.adp[avail])[:60]]
        mult = self._need_multiplier(counts, picks_left)
        pos_mult = np.array([mult.get(self.pos[i], 1.0) for i in window])
        # Lower score = picked sooner. Gumbel noise scaled by the market's own
        # disagreement reproduces reaches and falls at the right frequency.
        noise = self.rng.gumbel(0.0, 1.0, size=len(window)) * self.adp_sd[window] * 0.6
        score = self.adp[window] + noise - 12.0 * np.log(np.maximum(pos_mult, 1e-3))
        return int(window[int(np.argmin(score))])

    def _best_for_me(self, taken: np.ndarray, counts: dict[str, int]) -> int | None:
        avail = np.flatnonzero(~taken)
        if len(avail) == 0:
            return None
        limits = self._pos_limits
        ok = np.array([counts.get(self.pos[i], 0) < limits.get(self.pos[i], 99) for i in avail])
        pool = avail[ok] if ok.any() else avail
        return int(pool[int(np.argmax(self.points[pool]))])

    # ------------------------------------------------------------ evaluation
    def evaluate_candidates(self, state: DraftState, candidates: list[str] | None = None,
                            n_sims: int = 200, top_k: int = 14,
                            horizon: int | None = None) -> pl.DataFrame:
        """Score each candidate by the strength of the roster it leads to.

        For every candidate we force that pick, simulate the remainder of the
        draft `n_sims` times, and record the best legal starting lineup the
        resulting roster produces. The difference between candidates is exactly
        the quantity a draft pick should be optimising.
        """
        taken0 = np.zeros(len(self.ids), dtype=bool)
        for pid in state.drafted:
            i = self.index.get(pid)
            if i is not None:
                taken0[i] = True

        if candidates is None:
            avail = np.flatnonzero(~taken0)
            order = avail[np.argsort(-self.points[avail])][: top_k * 3]
            # Consider the best available at each position plus the best overall.
            picked: list[int] = []
            seen_pos: dict[str, int] = {}
            for i in order:
                p = self.pos[i]
                if seen_pos.get(p, 0) < 4:
                    seen_pos[p] = seen_pos.get(p, 0) + 1
                    picked.append(int(i))
                if len(picked) >= top_k:
                    break
            cand_idx = picked
        else:
            cand_idx = [self.index[c] for c in candidates if c in self.index]

        slots = self.league.starters
        rows = []
        for ci in cand_idx:
            scores = np.empty(n_sims)
            taken = taken0.copy()
            taken[ci] = True
            for s in range(n_sims):
                roster = self.simulate_remaining(taken, state, my_extra=[ci], max_picks=horizon)
                scores[s] = optimal_lineup_points(self.points[roster], self.pos[roster], slots)
            rows.append({
                "player_id": self.ids[ci],
                "player_name": self.names[ci],
                "position": self.pos[ci],
                "proj_points": float(self.points[ci]),
                "adp": float(self.adp[ci]),
                "roster_value": float(scores.mean()),
                "roster_value_sd": float(scores.std()),
            })
        out = pl.DataFrame(rows).sort("roster_value", descending=True)
        if out.height:
            best = out["roster_value"][0]
            out = out.with_columns((pl.col("roster_value") - best).alias("value_vs_best"))
        return out
