"""Correlated Monte Carlo simulation of NFL weeks.

Generative model, per team per week
-----------------------------------
1.  **Game outcome.** margin ~ N(spread, 12.73), total ~ N(total, 13.19).
    Team points = (total +/- margin)/2. Both residuals come straight off the
    closing line, so the market's information is the backbone of the sim.
2.  **Volume.** plays ~ N(expected + 0.086*margin, 8.4);
    pass rate = neutral rate - 0.0043*margin + N(0, 0.062). Sacks are binomial
    on dropbacks; attempts and carries fall out of the split.
3.  **Usage.** Each team's target / carry / dropback shares are drawn from a
    Dirichlet centred on the projected shares (kappa = 25 / 11 / 120, fit from
    week-to-week share variance). Inactive players are zeroed *before*
    normalisation, so an injury genuinely redistributes volume to teammates.
4.  **Yardage.** Per-play yards are shifted gammas fit on 2023-25 play-by-play
    (receiving: Gamma(2.084) - 3; rushing: Gamma(1.552) - 3), scaled per player
    so the mean matches his own efficiency. Because a gamma sum is a gamma,
    a whole game's yardage is one draw, not a loop over plays.
5.  **Touchdowns.** Team offensive TDs = -0.44 + 0.125 * team points + N(0,0.64),
    split pass/rush at 61.5%, then allocated across the roster in proportion to
    share x relative scoring rate.

Correlation is *structural*, not bolted on: teammates share one points draw, one
play count and one target pie, and a quarterback's passing yards are literally
the sum of his receivers' receiving yards. Stacks, game-script effects and the
negative correlation between a team's own pass catchers all emerge for free.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import polars as pl

from ..config import DEFAULT_SIMS, REGULAR_SEASON_WEEKS, sim_chunk_size
from ..models.availability import AvailabilityModel, availability_params
from ..models.projections import ProjectionSet
from ..scoring import LeagueSettings, Scoring
from .backend import ArrayBackend, make_backend
from .constants import CALIBRATION, Calibration

log = logging.getLogger(__name__)

SKILL = ("QB", "RB", "WR", "TE")


@dataclass
class SimInputs:
    """Flat arrays describing the player pool and the season's team-weeks."""

    player_ids: list[str]
    player_names: list[str]
    positions: np.ndarray
    teams: list[str]
    team_index: np.ndarray            # (P,) index into `team_list`
    team_list: list[str]

    target_share: np.ndarray
    carry_share: np.ndarray
    dropback_share: np.ndarray
    catch_rate: np.ndarray
    yards_per_target: np.ndarray
    rush_ypc: np.ndarray
    rec_td_rate: np.ndarray
    rush_td_rate: np.ndarray
    int_rate: np.ndarray
    sack_rate: np.ndarray
    fumble_rate: np.ndarray
    is_te: np.ndarray
    is_qb: np.ndarray
    is_kicker: np.ndarray
    is_dst: np.ndarray
    fg_att_pg: np.ndarray
    pat_att_pg: np.ndarray
    dst_quality: np.ndarray

    availability: AvailabilityModel
    role_confidence: np.ndarray

    # team-week tables, indexed [week, team]
    weeks: np.ndarray                 # (W,) week numbers
    tw_points: np.ndarray             # (W, T) implied points
    tw_margin: np.ndarray             # (W, T) expected margin (team perspective)
    tw_plays: np.ndarray              # (W, T)
    tw_pass_rate: np.ndarray          # (W, T)
    tw_total: np.ndarray              # (W, T) game total line
    tw_playing: np.ndarray            # (W, T) 1 if the team plays that week
    tw_opponent: np.ndarray           # (W, T) opponent team index, -1 on bye
    tw_def_pass: np.ndarray           # (W, T) opponent pass defence factor
    tw_def_rush: np.ndarray           # (W, T) opponent rush defence factor

    # Games are the unit the score is actually sampled on: one margin and one
    # total per game, split between the two teams. Sampling each team
    # independently would let both sides of a game "win" in the same simulation.
    games_home: list[np.ndarray] = field(default_factory=list)   # per week, (G,) team idx
    games_away: list[np.ndarray] = field(default_factory=list)
    games_spread: list[np.ndarray] = field(default_factory=list)  # home margin
    games_total: list[np.ndarray] = field(default_factory=list)
    games_id: list[list[str]] = field(default_factory=list)

    @property
    def n_players(self) -> int:
        return len(self.player_ids)

    @property
    def n_teams(self) -> int:
        return len(self.team_list)


def _col(df: pl.DataFrame, name: str, default: float) -> np.ndarray:
    if name not in df.columns:
        return np.full(df.height, default, dtype=np.float64)
    return np.nan_to_num(df[name].cast(pl.Float64).to_numpy(), nan=default)


def build_sim_inputs(proj: ProjectionSet, min_ppg: float = 0.15) -> SimInputs:
    """Flatten a :class:`ProjectionSet` into simulation-ready arrays."""
    p = proj.players
    if "blended_ppg" in p.columns:
        p = p.filter(pl.col("blended_ppg").fill_null(0.0) >= min_ppg)
    # A total order on the player axis: the simulation indexes RNG draws by row,
    # so an unstable sort makes an otherwise seeded run irreproducible.
    if "player_id" in p.columns:
        p = p.sort("player_id")
    teams = sorted(set(proj.team_weeks["team"].to_list()))
    t_index = {t: i for i, t in enumerate(teams)}
    p = p.filter(pl.col("team").is_in(teams))

    team_idx = np.array([t_index[t] for t in p["team"].to_list()], dtype=np.int64)
    positions = p["position"].to_numpy().astype(str)

    weeks = np.array(sorted(set(proj.team_weeks["week"].to_list())), dtype=np.int64)
    W, T = len(weeks), len(teams)
    w_index = {int(w): i for i, w in enumerate(weeks)}

    tw_points = np.full((W, T), np.nan)
    tw_margin = np.zeros((W, T))
    tw_plays = np.full((W, T), 62.0)
    tw_pass_rate = np.full((W, T), 0.57)
    tw_total = np.full((W, T), 44.0)
    tw_playing = np.zeros((W, T))
    tw_opponent = np.full((W, T), -1, dtype=np.int64)
    tw_def_pass = np.ones((W, T))
    tw_def_rush = np.ones((W, T))

    for row in proj.team_weeks.iter_rows(named=True):
        wi = w_index.get(int(row["week"]))
        ti = t_index.get(row["team"])
        if wi is None or ti is None:
            continue
        tw_points[wi, ti] = row.get("implied_points") or np.nan
        tw_margin[wi, ti] = row.get("team_spread") or 0.0
        tw_plays[wi, ti] = row.get("exp_plays_game") or 62.0
        tw_pass_rate[wi, ti] = row.get("exp_pass_rate_neutral") or 0.57
        tw_total[wi, ti] = row.get("total_line") or 44.0
        tw_playing[wi, ti] = 1.0
        opp = t_index.get(row.get("opponent"))
        if opp is not None:
            tw_opponent[wi, ti] = opp
        tw_def_pass[wi, ti] = row.get("opp_def_pass_factor") or 1.0
        tw_def_rush[wi, ti] = row.get("opp_def_rush_factor") or 1.0

    tw_points = np.nan_to_num(tw_points, nan=22.5)

    games_home, games_away, games_spread, games_total, games_id = [], [], [], [], []
    sched = proj.team_weeks
    for w in weeks:
        rows = sched.filter((pl.col("week") == int(w)) & (pl.col("is_home") == 1.0))
        h, a, sp, to, gid = [], [], [], [], []
        for row in rows.iter_rows(named=True):
            hi = t_index.get(row["team"])
            ai = t_index.get(row.get("opponent"))
            if hi is None or ai is None:
                continue
            h.append(hi)
            a.append(ai)
            spread = row.get("team_spread")
            total = row.get("total_line")
            if spread is None or not np.isfinite(spread):
                spread = float(tw_points[w_index[int(w)], hi] - tw_points[w_index[int(w)], ai])
            if total is None or not np.isfinite(total):
                total = float(tw_points[w_index[int(w)], hi] + tw_points[w_index[int(w)], ai])
            sp.append(float(spread))
            to.append(float(total))
            gid.append(str(row.get("game_id") or f"{w}_{row['team']}"))
        games_home.append(np.asarray(h, dtype=np.int64))
        games_away.append(np.asarray(a, dtype=np.int64))
        games_spread.append(np.asarray(sp, dtype=np.float64))
        games_total.append(np.asarray(to, dtype=np.float64))
        games_id.append(gid)

    avail = availability_params(
        positions,
        ages=_col(p, "age", 26.0),
        games_missed_rate=p["games_missed_rate"].cast(pl.Float64).to_numpy()
        if "games_missed_rate" in p.columns else None,
    )

    return SimInputs(
        player_ids=p["player_id"].to_list(),
        player_names=p["player_name"].to_list(),
        positions=positions,
        teams=p["team"].to_list(),
        team_index=team_idx,
        team_list=teams,
        target_share=_col(p, "target_share_proj", 0.0),
        carry_share=_col(p, "carry_share_proj", 0.0),
        dropback_share=_col(p, "dropback_share_proj", 0.0),
        catch_rate=np.clip(_col(p, "catch_rate", 0.65), 0.3, 0.92),
        yards_per_target=np.clip(_col(p, "yards_per_target", 7.5), 1.5, 16.0),
        rush_ypc=np.clip(_col(p, "rush_ypc", 4.3), 1.5, 8.0),
        rec_td_rate=np.clip(_col(p, "rec_td_rate", 0.05), 0.005, 0.2),
        rush_td_rate=np.clip(_col(p, "rush_td_rate", 0.03), 0.002, 0.15),
        int_rate=np.clip(_col(p, "int_rate", 0.024), 0.005, 0.06),
        sack_rate=np.clip(_col(p, "sack_rate", CALIBRATION.league_sack_rate), 0.01, 0.16),
        fumble_rate=np.clip(_col(p, "fumble_rate", 0.0055), 0.0, 0.03),
        is_te=(positions == "TE").astype(np.float64),
        is_qb=(positions == "QB").astype(np.float64),
        is_kicker=(positions == "K").astype(np.float64),
        is_dst=(positions == "DST").astype(np.float64),
        fg_att_pg=_col(p, "fg_att_pg", 0.0),
        pat_att_pg=_col(p, "pat_att_pg", 0.0),
        dst_quality=_col(p, "quality", 1.0),
        availability=avail,
        role_confidence=np.clip(_col(p, "role_confidence", 0.4), 0.0, 1.0),
        weeks=weeks,
        tw_points=tw_points, tw_margin=tw_margin, tw_plays=tw_plays,
        tw_pass_rate=tw_pass_rate, tw_total=tw_total, tw_playing=tw_playing,
        tw_opponent=tw_opponent, tw_def_pass=tw_def_pass, tw_def_rush=tw_def_rush,
        games_home=games_home, games_away=games_away, games_spread=games_spread,
        games_total=games_total, games_id=games_id,
    )


@dataclass
class WeekResult:
    """Simulated fantasy points (and team scores) for one week."""

    points: np.ndarray          # (S, P)
    team_points: np.ndarray     # (S, T)
    active: np.ndarray          # (S, P) bool
    week: int
    stats: dict[str, np.ndarray] = field(default_factory=dict)


class MonteCarloEngine:
    """Vectorised week/season simulator.

    Parameters
    ----------
    inputs
        Flattened projection arrays.
    scoring
        League scoring, applied inside the chunk so no stat matrix ever leaves
        the device unless explicitly requested.
    seed
        Base RNG seed; each chunk advances it so results are reproducible.
    """

    def __init__(self, inputs: SimInputs, scoring: Scoring | None = None,
                 seed: int | None = None, backend: str | None = None,
                 calibration: Calibration | None = None):
        self.inp = inputs
        self.scoring = scoring or Scoring()
        self.cal = calibration or CALIBRATION
        self.seed = seed
        self.xp: ArrayBackend = make_backend(seed=seed, prefer=backend)
        self._onehot = self._build_onehot()
        self._role_conf = np.asarray(inputs.role_confidence, dtype=np.float64)

    # ---------------------------------------------------------------- helpers
    def _build_onehot(self):
        """(P, T) matrix used to sum player quantities up to their team."""
        P, T = self.inp.n_players, self.inp.n_teams
        oh = np.zeros((P, T), dtype=np.float32)
        oh[np.arange(P), self.inp.team_index] = 1.0
        return self.xp.asarray(oh)

    def _team_sum(self, x):
        """(S, P) -> (S, T): total per team."""
        return self.xp.matmul(x, self._onehot)

    def _spread_to_players(self, team_vals):
        """(S, T) -> (S, P): broadcast a team quantity to its players."""
        return self.xp.take(team_vals, self.inp.team_index, axis=1)

    def _dirichlet_shares(self, base, kappa: float, active, n_sims: int, shock=None):
        """Draw per-team shares from a Dirichlet centred on `base`.

        `base` is (P,) projected share; inactive players are zeroed first so the
        remaining volume is genuinely redistributed to their teammates. An
        optional season-level `shock` tilts the centre of the Dirichlet for the
        whole season rather than one week.
        """
        xp = self.xp
        base_arr = xp.asarray(np.maximum(base, 0.0))[None, :]
        alpha = base_arr * kappa * active
        if shock is not None:
            # Normalise the season shock by its share-weighted team mean so the
            # Dirichlet's expected shares still equal the projection. Without
            # this the log-normal draw quietly inflates every high-share player
            # (Jensen), and the whole board drifts up by ~10%.
            w = base_arr * active
            mean_shock = self._team_sum(w * shock) / xp.maximum(self._team_sum(w), 1e-9)
            alpha = alpha * shock / xp.maximum(self._spread_to_players(mean_shock), 1e-6)
        g = xp.gamma(alpha + 1e-9, 1.0) * active
        denom = self._spread_to_players(self._team_sum(g))
        return g / xp.maximum(denom, 1e-9)

    # ------------------------------------------------------------------- core
    def season_shocks(self, n_sims: int, role_sd_floor: float | None = None,
                      role_sd_span: float | None = None, eff_sd: float | None = None):
        """Draw one latent role/efficiency multiplier per player per season.

        Week-to-week noise alone produces a leaderboard that is far too flat:
        it never lets a WR2 seize the WR1 role for a year, or a back lose his
        job in September. The real preseason uncertainty is mostly about *which
        season a player is going to have*, not which week -- so each simulated
        season draws a persistent multiplier, log-normal and mean-one so team
        volume is untouched, with a spread that widens for players whose role
        the history does not pin down.
        """
        xp = self.xp
        role_sd_floor = self.cal.role_shock_floor if role_sd_floor is None else role_sd_floor
        role_sd_span = self.cal.role_shock_span if role_sd_span is None else role_sd_span
        eff_sd = self.cal.efficiency_shock_sd if eff_sd is None else eff_sd
        conf = np.clip(self._role_conf, 0.0, 1.0)
        sd_role = role_sd_floor + role_sd_span * (1.0 - conf)
        # Computed on whichever device the backend uses; copying to the host to
        # call numpy's exp would cost two transfers per chunk for no reason.
        sd = xp.asarray(sd_role)[None, :]
        z = xp.normal((n_sims, self.inp.n_players), 0.0, 1.0)
        role = xp.exp(z * sd - 0.5 * sd * sd)
        z2 = xp.normal((n_sims, self.inp.n_players), 0.0, 1.0)
        eff = xp.exp(z2 * eff_sd - 0.5 * eff_sd ** 2)
        return role, eff

    def simulate_week(self, week_pos: int, n_sims: int, active_state=None,
                      collect_stats: bool = False, role_shock=None,
                      eff_shock=None, team_shock=None, td_share_shock=None) -> WeekResult:
        """Simulate `n_sims` independent realisations of one week."""
        xp = self.xp
        inp, cal = self.inp, self.cal
        P = inp.n_players
        wi = week_pos

        playing_team = xp.asarray(inp.tw_playing[wi])[None, :]           # (1, T)
        playing = self._spread_to_players(playing_team)                   # (S=1, P)

        # --- availability -------------------------------------------------
        if active_state is None:
            active = (xp.uniform((n_sims, P)) < xp.asarray(inp.availability.healthy_rate)[None, :])
            active = xp.asarray(active) * 1.0
        else:
            active = active_state
        active = active * playing

        # --- game outcome -------------------------------------------------
        # One margin and one total per *game*, then split between the two teams,
        # so a simulated week is a coherent set of results rather than 32
        # independent scores.
        team_points, margin = self._sample_game_scores(wi, n_sims, team_shock)
        team_points = team_points * playing_team

        # --- team volume --------------------------------------------------
        exp_plays = xp.asarray(inp.tw_plays[wi])[None, :]
        plays = xp.clip(
            xp.normal((n_sims, inp.n_teams), exp_plays + cal.plays_per_margin * margin, cal.plays_sd),
            35.0, 92.0) * playing_team
        neutral_pr = xp.asarray(inp.tw_pass_rate[wi])[None, :]
        pass_rate = xp.clip(
            xp.normal((n_sims, inp.n_teams), neutral_pr + cal.pass_rate_per_margin * margin,
                      cal.pass_rate_sd), 0.22, 0.86)

        dropbacks = plays * pass_rate
        carries_team = xp.maximum(plays - dropbacks, 0.0)

        # --- usage allocation ---------------------------------------------
        tgt_share = self._dirichlet_shares(inp.target_share, cal.target_share_kappa, active,
                                           n_sims, shock=role_shock)
        car_share = self._dirichlet_shares(inp.carry_share, cal.carry_share_kappa, active,
                                           n_sims, shock=role_shock)
        db_share = self._dirichlet_shares(inp.dropback_share, cal.dropback_share_kappa, active,
                                          n_sims)

        team_sack_rate = self._team_sum(db_share * xp.asarray(inp.sack_rate)[None, :])
        sacks_team = xp.binomial(self.xp.round(dropbacks), team_sack_rate)
        attempts_team = xp.maximum(dropbacks - sacks_team, 0.0)

        attempts_p = self._spread_to_players(attempts_team)
        carries_p = self._spread_to_players(carries_team)

        targets = xp.poisson(attempts_p * tgt_share)
        carries = xp.poisson(carries_p * car_share)
        pass_att = attempts_p * db_share

        # --- yardage ------------------------------------------------------
        def_pass = self._spread_to_players(xp.asarray(inp.tw_def_pass[wi])[None, :])
        def_rush = self._spread_to_players(xp.asarray(inp.tw_def_rush[wi])[None, :])

        catch_rate = xp.asarray(inp.catch_rate)[None, :]
        receptions = xp.binomial(targets, catch_rate)

        eff = 1.0 if eff_shock is None else eff_shock
        ypr = xp.asarray(inp.yards_per_target / np.maximum(inp.catch_rate, 1e-3))[None, :] * def_pass * eff
        rec_scale = (ypr + cal.rec_yards_shift) / cal.rec_yards_shape
        rec_yards = (xp.gamma(receptions * cal.rec_yards_shape, rec_scale)
                     - receptions * cal.rec_yards_shift)

        ypc = xp.asarray(inp.rush_ypc)[None, :] * def_rush * eff
        rush_scale = (ypc + cal.rush_yards_shift) / cal.rush_yards_shape
        rush_yards = (xp.gamma(carries * cal.rush_yards_shape, rush_scale)
                      - carries * cal.rush_yards_shift)

        # Passing yards are the team's receiving yards, split by attempt share --
        # this is what makes a quarterback and his receivers move together.
        team_rec_yards = self._team_sum(rec_yards)
        pass_yards = self._spread_to_players(team_rec_yards) * db_share
        interceptions = xp.binomial(pass_att, xp.asarray(inp.int_rate)[None, :])

        # --- touchdowns ----------------------------------------------------
        td_mean = cal.td_intercept + cal.td_slope * team_points
        team_tds = xp.maximum(self.xp.round(xp.normal((n_sims, inp.n_teams), td_mean, cal.td_sd)), 0.0)
        if td_share_shock is None:
            pass_share = cal.pass_td_share
        else:
            pass_share = xp.clip(td_share_shock, 0.25, 0.90)
        team_pass_tds = xp.binomial(team_tds, pass_share)
        team_rush_tds = xp.maximum(team_tds - team_pass_tds, 0.0)

        rec_w = tgt_share * xp.asarray(inp.rec_td_rate / max(np.mean(inp.rec_td_rate[inp.rec_td_rate > 0]), 1e-6))[None, :]
        rec_w = rec_w * active
        rec_w = rec_w / xp.maximum(self._spread_to_players(self._team_sum(rec_w)), 1e-9)
        rec_tds = xp.binomial(self._spread_to_players(team_pass_tds), rec_w)

        rush_w = car_share * xp.asarray(inp.rush_td_rate / max(np.mean(inp.rush_td_rate[inp.rush_td_rate > 0]), 1e-6))[None, :]
        rush_w = rush_w * active
        rush_w = rush_w / xp.maximum(self._spread_to_players(self._team_sum(rush_w)), 1e-9)
        rush_tds = xp.binomial(self._spread_to_players(team_rush_tds), rush_w)

        pass_tds = self._spread_to_players(team_pass_tds) * db_share

        touches = carries + receptions
        fumbles = xp.binomial(touches, xp.asarray(inp.fumble_rate)[None, :])

        # --- kickers & defences --------------------------------------------
        s = self.scoring
        is_k = xp.asarray(inp.is_kicker)[None, :]
        is_d = xp.asarray(inp.is_dst)[None, :]
        kicker_pts = self._kicker_points(team_points, n_sims) * is_k
        dst_pts = self._dst_points(wi, team_points, n_sims) * is_d

        # --- score -----------------------------------------------------------
        pts = (
            pass_yards * s.passing_yards
            + pass_tds * s.passing_tds
            + interceptions * s.passing_interceptions
            + rush_yards * s.rushing_yards
            + rush_tds * s.rushing_tds
            + receptions * s.receptions
            + rec_yards * s.receiving_yards
            + rec_tds * s.receiving_tds
            + fumbles * s.fumbles_lost
        )
        if s.te_premium:
            pts = pts + receptions * s.te_premium * xp.asarray(inp.is_te)[None, :]
        for threshold, bonus, arr in ((300, s.pass_300_bonus, pass_yards),
                                      (400, s.pass_400_bonus, pass_yards),
                                      (100, s.rush_100_bonus, rush_yards),
                                      (100, s.rec_100_bonus, rec_yards)):
            if bonus:
                pts = pts + (arr >= threshold) * bonus

        skill_mask = xp.asarray(((inp.is_kicker + inp.is_dst) == 0).astype(np.float32))[None, :]
        pts = pts * skill_mask + kicker_pts + dst_pts
        pts = pts * active

        stats: dict[str, np.ndarray] = {}
        if collect_stats:
            for name, arr in (("targets", targets), ("receptions", receptions),
                              ("receiving_yards", rec_yards), ("receiving_tds", rec_tds),
                              ("carries", carries), ("rushing_yards", rush_yards),
                              ("rushing_tds", rush_tds), ("passing_yards", pass_yards),
                              ("passing_tds", pass_tds), ("interceptions", interceptions)):
                stats[name] = xp.to_numpy(arr * active)

        return WeekResult(
            points=xp.to_numpy(pts),
            team_points=xp.to_numpy(team_points),
            active=xp.to_numpy(active) > 0.5,
            week=int(inp.weeks[wi]),
            stats=stats,
        )

    def _sample_game_scores(self, wi: int, n_sims: int, team_shock=None):
        """Sample (points, margin) for every team playing in week `wi`.

        Returns
        -------
        team_points : (S, T)
            Points scored, zero for teams on bye.
        team_margin : (S, T)
            Each team's own margin (its points minus its opponent's), which is
            what the game-script terms key off.
        """
        xp, inp, cal = self.xp, self.inp, self.cal
        home = inp.games_home[wi]
        away = inp.games_away[wi]
        n_games = len(home)
        team_points = xp.zeros((n_sims, inp.n_teams))
        team_margin = xp.zeros((n_sims, inp.n_teams))
        if n_games == 0:
            return team_points, team_margin

        spread = xp.asarray(inp.games_spread[wi])[None, :]
        total = xp.asarray(inp.games_total[wi])[None, :]
        if team_shock is not None:
            # A season-long team-strength surprise moves that team's points; the
            # game's total and margin follow from the two teams' shocks.
            sh_home = xp.take(team_shock, home, axis=1)
            sh_away = xp.take(team_shock, away, axis=1)
            total = total + sh_home + sh_away
            spread = spread + sh_home - sh_away

        margin = xp.normal((n_sims, n_games), spread, cal.margin_sd)
        game_total = xp.maximum(xp.normal((n_sims, n_games), total, cal.total_sd), 3.0)
        home_pts = xp.clip((game_total + margin) / 2.0, 0.0, 80.0)
        away_pts = xp.clip((game_total - margin) / 2.0, 0.0, 80.0)

        team_points = self._scatter_teams(team_points, home, home_pts)
        team_points = self._scatter_teams(team_points, away, away_pts)
        team_margin = self._scatter_teams(team_margin, home, home_pts - away_pts)
        team_margin = self._scatter_teams(team_margin, away, away_pts - home_pts)
        return team_points, team_margin

    def _scatter_teams(self, dest, idx: np.ndarray, values):
        """Write (S, G) per-game values into the (S, T) team columns `idx`."""
        return self.xp.scatter_columns(dest, idx, values)

    def _kicker_points(self, team_points, n_sims: int):
        """Field goals scale with the team's scoring; PATs follow its touchdowns."""
        xp, inp, s, cal = self.xp, self.inp, self.scoring, self.cal
        tp = self._spread_to_players(team_points)
        fg_att = xp.maximum(cal.fg_att_intercept + cal.fg_att_slope * tp, 0.0)
        fg_att = xp.poisson(fg_att)
        # Split attempts by the league distance mix, then make them.
        from ..models.special import FG_DISTANCE_MIX, FG_MAKE_RATE

        pts = xp.zeros(fg_att.shape if hasattr(fg_att, "shape") else (n_sims, inp.n_players))
        remaining = fg_att
        for i, (bucket, mix) in enumerate(FG_DISTANCE_MIX.items()):
            share = mix if i < len(FG_DISTANCE_MIX) - 1 else 1.0
            att = xp.binomial(remaining, min(share / max(1.0 - sum(list(FG_DISTANCE_MIX.values())[:i]), 1e-6), 1.0))
            remaining = xp.maximum(remaining - att, 0.0)
            made = xp.binomial(att, FG_MAKE_RATE[bucket])
            value = {"0_39": s.fg_0_39, "40_49": s.fg_40_49, "50_plus": s.fg_50_plus}[bucket]
            pts = pts + made * value + (att - made) * s.fg_miss
        pat_att = xp.poisson(xp.maximum((tp - 3.0 * xp.asarray(fg_att)) / 7.0, 0.0))
        pat_made = xp.binomial(pat_att, cal.pat_rate)
        return pts + pat_made * s.pat_made

    def _dst_points(self, wi: int, team_points, n_sims: int):
        """Defence scoring keys off what the *opponent* actually scored."""
        xp, inp, s = self.xp, self.inp, self.scoring
        opp_idx = inp.tw_opponent[wi]
        safe_opp = np.where(opp_idx >= 0, opp_idx, 0)
        opp_points_team = xp.take(team_points, safe_opp, axis=1)
        pa = self._spread_to_players(opp_points_team)

        from ..models.special import DST_BASE

        # Rates must be broadcast to (n_sims, P) *before* sampling -- drawing at
        # (1, P) would hand every simulation the same defensive stat line.
        ones = xp.full((n_sims, 1), 1.0)
        q = ones * xp.asarray(inp.dst_quality)[None, :]
        sacks = xp.poisson(q * DST_BASE["sacks"])
        ints = xp.poisson(q * DST_BASE["interceptions"])
        fums = xp.poisson(q * DST_BASE["fumble_recoveries"])
        tds = xp.poisson(q * DST_BASE["tds"])
        safeties = xp.poisson(ones * xp.full((1, inp.n_players), DST_BASE["safeties"]))

        pa_pts = (
            (pa < 1) * s.dst_points_allowed_0
            + ((pa >= 1) & (pa < 7)) * s.dst_points_allowed_1_6
            + ((pa >= 7) & (pa < 14)) * s.dst_points_allowed_7_13
            + ((pa >= 14) & (pa < 21)) * s.dst_points_allowed_14_20
            + ((pa >= 21) & (pa < 28)) * s.dst_points_allowed_21_27
            + ((pa >= 28) & (pa < 35)) * s.dst_points_allowed_28_34
            + (pa >= 35) * s.dst_points_allowed_35_plus
        )
        return (sacks * s.dst_sack + ints * s.dst_interception + fums * s.dst_fumble_recovery
                + tds * s.dst_td + safeties * s.dst_safety + pa_pts)

    # ----------------------------------------------------------------- season
    def simulate_season(self, n_sims: int = DEFAULT_SIMS, weeks: list[int] | None = None,
                        chunk: int | None = None, keep_weekly: bool = True,
                        progress=None) -> "SeasonResult":
        """Simulate whole seasons with a persistent per-player injury state.

        Returns per-player season totals plus, optionally, the full
        (weeks x sims x players) weekly points needed for lineup optimisation.
        """
        inp = self.inp
        xp = self.xp
        week_positions = list(range(len(inp.weeks))) if weeks is None else \
            [i for i, w in enumerate(inp.weeks) if int(w) in set(weeks)]
        P = inp.n_players
        chunk = chunk or sim_chunk_size(P, self.xp.backend)
        chunk = min(chunk, n_sims)

        totals = np.zeros((n_sims, P), dtype=np.float32)
        games = np.zeros((n_sims, P), dtype=np.float32)
        weekly = np.zeros((len(week_positions), n_sims, P), dtype=np.float32) if keep_weekly else None

        done = 0
        while done < n_sims:
            size = min(chunk, n_sims - done)
            active = (xp.uniform((size, P)) < xp.asarray(inp.availability.healthy_rate)[None, :])
            active = xp.asarray(active) * 1.0
            p_hurt = xp.asarray(inp.availability.p_get_hurt)[None, :]
            p_out = xp.asarray(inp.availability.p_stay_out)[None, :]
            role_shock, eff_shock = self.season_shocks(size)
            # One persistent team-strength surprise per simulated season: the
            # market's preseason expectation carries a residual sd of ~2.6
            # points per game, and that error persists all year.
            team_shock = xp.normal((size, inp.n_teams), 0.0, self.cal.team_strength_sd)
            # Some offences score through the air all year and some on the ground;
            # that split is a season-level property, not weekly noise.
            base_logit = float(np.log(self.cal.pass_td_share / (1 - self.cal.pass_td_share)))
            logit = xp.normal((size, inp.n_teams), base_logit, self.cal.pass_td_share_logit_sd)
            td_share_shock = 1.0 / (1.0 + xp.exp(-logit))

            for wpos_i, wpos in enumerate(week_positions):
                # Persistent injury state: absences last multiple weeks.
                u = xp.uniform((size, P))
                stay_out = xp.asarray(u < p_out) * 1.0
                get_hurt = xp.asarray(u < p_hurt) * 1.0
                active = active * (1.0 - get_hurt) + (1.0 - active) * (1.0 - stay_out)

                res = self.simulate_week(wpos, size, active_state=active,
                                         role_shock=role_shock, eff_shock=eff_shock,
                                         team_shock=team_shock)
                totals[done:done + size] += res.points
                games[done:done + size] += res.active.astype(np.float32)
                if weekly is not None:
                    weekly[wpos_i, done:done + size] = res.points
            done += size
            if progress is not None:
                progress(done, n_sims)

        return SeasonResult(
            totals=totals, games=games, weekly=weekly,
            weeks=[int(inp.weeks[i]) for i in week_positions],
            player_ids=inp.player_ids, player_names=inp.player_names,
            positions=inp.positions, teams=inp.teams,
        )


@dataclass
class SeasonResult:
    """Season-long simulation output."""

    totals: np.ndarray           # (S, P) season fantasy points
    games: np.ndarray            # (S, P) games played
    weekly: np.ndarray | None    # (W, S, P) weekly points
    weeks: list[int]
    player_ids: list[str]
    player_names: list[str]
    positions: np.ndarray
    teams: list[str]

    def percentiles(self, qs=(5, 25, 50, 75, 95)) -> pl.DataFrame:
        """Per-player summary of the season distribution."""
        pct = np.percentile(self.totals, qs, axis=0)
        ppg_den = np.maximum(self.games.mean(axis=0), 1e-6)
        data = {
            "player_id": self.player_ids,
            "player_name": self.player_names,
            "position": list(self.positions),
            "team": self.teams,
            "proj_points": self.totals.mean(axis=0),
            "proj_sd": self.totals.std(axis=0),
            "proj_games": self.games.mean(axis=0),
            "proj_ppg": self.totals.mean(axis=0) / ppg_den,
        }
        for i, q in enumerate(qs):
            data[f"p{q}"] = pct[i]
        return pl.DataFrame(data).sort("proj_points", descending=True)
