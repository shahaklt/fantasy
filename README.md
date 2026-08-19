# Gridiron

A local Monte Carlo engine for fantasy football and NFL prediction markets.

It simulates the NFL season play-by-play-style — one coherent margin and total
per game, with every player's stat line drawn inside it — and reads that
simulation out three ways: as season-long **projections**, as a live
**draft-day assistant**, and as **fair value** for contracts listed on Kalshi
and Polymarket.

Everything runs on your machine. The only outbound calls are to public data
sources and, if you configure them, the trading venues.

---

## What it does

**Projections & simulation**
- Full-season Monte Carlo for every rostered skill player, kicker and defence
- Distributions, not point estimates: floor, ceiling, boom/bust rates, week-by-week
- Team-level context from the published Vegas spread and total for all 272 games
- Injury modelled as a persistent two-state chain, so seasons genuinely get lost
- CUDA acceleration via CuPy (torch as a fallback); NumPy otherwise

**Draft day**
- VORP / VOLS baselines from your exact roster and scoring settings
- Auction dollar values that sum to the money actually in the room
- Tiers found from real gaps in value, not fixed bucket sizes
- P(still available) at each of your upcoming picks, from consensus rank dispersion
- **Best-pick recommender**: for each candidate it simulates the rest of the
  draft — opponents reaching, falling and filling needs — and keeps the pick
  that leaves the strongest starting lineup

**Games & markets**
- Predicted score, margin and total per game with the full distribution
- Drill-down: how each team's points decompose, and every player's contribution
- Alternate spreads, totals and team totals priced off the simulation
- Devigging (multiplicative / additive / power / Shin / logarithmic), Kelly
  sizing with correlation, calibration scoring and closing-line value
- Live order-book tape: micro-price, book imbalance, order-flow imbalance,
  realised volatility and momentum

**Automation**
- Data auto-refreshes twice daily at 00:00 and 12:00 local

---

## Quick start

```bash
git clone <this repo> && cd fantasy
./run.sh                     # Windows: run.bat
```

Then open <http://127.0.0.1:8000>.

The first launch creates a virtualenv, installs dependencies, downloads roughly
150 MB of nflverse data and runs the first simulation (about a minute on a
modern desktop). The draft board is usable while that finishes.

## Updating

```bash
./update.sh          # Windows: update.bat
```

Pulls the latest code and syncs dependencies **only if they changed**. Nothing
re-downloads: your NFL data cache, league settings, saved credentials and tick
database all live in gitignored directories (`data/cache`, `data/artifacts`,
`data/user`) that git never touches.

It refuses to run over uncommitted changes, and fast-forwards only — if your
branch has diverged it tells you rather than guessing.

By hand it is two commands:

```bash
git pull
pip install -e ".[dev]"     # only needed when pyproject.toml changed
```

The package is installed editable, so code changes are live the moment you pull;
pip only has to run when the dependency list itself moves. After a model change,
rebuild the simulations — `gridiron build --sims 20000`, or press **Rebuild
sims** in the web app. That re-runs the maths against data you already have.

To update on a different branch: `BRANCH=main ./update.sh`.

---

### Manual install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pip install -e ".[gpu]"        # optional: CuPy for CUDA 12.x
gridiron serve
```

### Getting the most out of a 12700K + RTX 3060 Ti

`run.sh` installs CuPy automatically when it sees `nvidia-smi`, picking the
wheel that matches your driver's CUDA major version. Confirm the GPU is in use
with `gridiron doctor`, which prints the device and its free VRAM; the dashboard
shows the active backend too.

With the GPU active, raise the simulation count well past the default:

```bash
gridiron build --sims 100000      # ~8 GB VRAM handles this comfortably
```

The engine chunks simulations to about 35% of VRAM and keeps every array on the
device for the whole chunk — the only transfer is the finished points matrix
coming back. On CPU it targets ~2 GB of RAM per chunk and leans on
multi-threaded BLAS.

**Why CuPy rather than torch.** CuPy exposes NumPy's own API, so the simulation
code is *the same code* on both paths — `ArrayBackend` is written against a
module reference and `CuPyBackend` overrides three methods (`to_numpy`, `index`,
`binomial`). There is no autograd machinery in the way, the install is a
fraction of torch's size, and import is far quicker. A `TorchBackend` remains as
a fallback for machines that already have torch and not CuPy; `make_backend`
prefers CuPy, then torch, then NumPy, falling back rather than failing so a
simulation always runs.

Note that CuPy owns the Monte Carlo hot loop only. A blanket
`import cupy as np` across the project would not work: `scipy.stats` and
`scipy.optimize` (the analytic game model, devigging, blend-weight fitting)
reject device arrays, Polars cannot build a frame from device memory, and JSON
serialisation needs host memory. The quant layer also works on a few hundred
elements at a time, where a kernel launch plus two transfers costs more than the
arithmetic saves. The split is deliberate.

| Variable | Effect |
| --- | --- |
| `GRIDIRON_DEVICE` | `auto` (default), `cupy`, `torch`, `cuda`, `numpy` |
| `GRIDIRON_DATA_DIR` | where caches and artifacts live |
| `GRIDIRON_TRADING_MODE` | `paper` (default) or `live` |
| `GRIDIRON_DEMO_VENUE` | `1` adds a synthetic venue for testing the live tape |

---

## Using it

### Learn the numbers first

**Stats Guide** (sidebar, or press <kbd>?</kbd>) documents all 38 stats the app
shows: what each one is, how to read it, and the trap. It opens with four ideas
the rest depends on, then a step-by-step draft-day order and the weekly
in-season loop, then a filterable reference grouped by where the stat appears.

If you read nothing else, read the four concepts at the top — most mistakes with
this tool come from treating a projection as a prediction, or comparing points
across positions.

### Set up your league first
**Settings** → teams, scoring preset, roster slots, your draft slot → *Save &
rebuild*. Everything downstream (replacement level, auction values, the
recommender) depends on these, so it is worth getting right before draft day.

### Draft day
Open **Draft Room** and mark each pick as it happens — yours and everyone
else's. The board removes drafted players, recomputes scarcity, and the `@N`
column shows the chance each player survives to your *next* pick.

Press **Recommend my pick** when you are on the clock. It runs a few hundred
full draft simulations per candidate and ranks them by the strength of the
roster each one leads to, which is not the same as ranking by projected points:
taking the best available player is often wrong when his position is deep and
another is about to run dry.

### Weekly
**Games** shows every matchup with predicted score against the market line.
Click one for the breakdown. **My League** holds your roster for start/sit
optimisation, which reports each player's *probability of being in your optimal
lineup* rather than a bare projection.

### Markets
**Markets** lists what the venues are quoting, prices each contract against the
simulation, and reports edge, blended probability and a Kelly-sized stake. Model
and market are combined in log-odds space with a capped shift — a liquid market
knows things your projections do not, and the cap stops one bad assumption from
producing an enormous position.

**Live Tape** streams order books and derives the microstructure signals.
Sentiment usually shows up as persistent order-flow imbalance before it reaches
the last-traded price.

### Getting around

| Key | Does |
| --- | --- |
| <kbd>⌘K</kbd> / <kbd>Ctrl K</kbd> | command palette — jump to any view, or search a player |
| <kbd>Alt</kbd>+<kbd>1…9</kbd> | jump straight to a view |
| <kbd>?</kbd> | open the Stats Guide |
| <kbd>Esc</kbd> | close the drawer or palette |

The strip under the header is a live machine readout: simulation state, run
count, engine, scheduler, trading mode and venue count. Numeric columns shade
themselves by percentile, so you can read the shape of a column without reading
any single number in it, and the **Range** column draws each player's floor,
median and ceiling on one rule.

---

## Trading (read this before enabling it)

**Paper mode is the default and nothing reaches an exchange in it.** Orders fill
against real books with slippage and fees so the workflow is honest, but no
money moves.

Going live requires all of:

1. `GRIDIRON_TRADING_MODE=live` in the environment
2. Credentials configured (below)
3. An explicit per-order confirmation

Every order passes a risk guard first: per-order notional, per-market exposure,
daily notional, open-position count, gross exposure as a fraction of bankroll,
and minimum edge. Limits are visible and editable in **Settings**. Creating the
file `data/user/STOP_TRADING` halts everything immediately — no restart needed.

### Kalshi
Create an API key in your Kalshi account, download the private key, then either
set environment variables:

```bash
export KALSHI_KEY_ID=<your key id>
export KALSHI_PRIVATE_KEY_PATH=/path/to/kalshi-key.pem
```

or enter the key id and path in **Settings**. Only the *path* is stored; the key
never leaves your machine and is sent nowhere but Kalshi. Requests are signed
RSA-PSS over `timestamp + METHOD + path`.

### Polymarket
```bash
export POLYMARKET_PRIVATE_KEY=<wallet key>
export POLYMARKET_FUNDER=<proxy address>      # for Magic/email wallets
pip install py-clob-client
```

Order signing is delegated to Polymarket's official client rather than
reimplemented.

Two things worth saying plainly. First, the exchange clients were written
against the published API specifications but could not be exercised against the
live endpoints from the machine that built them — run `gridiron doctor` and
place one small paper order, then one small live order, before trusting either
with size. Second, an edge measured against your own model is not an edge until
it survives calibration; the tooling to check that (Brier score, reliability
curves, closing-line value) is in `gridiron.quant.calibration`, and CLV is the
metric that matters.

---

## How the model works

**Team context.** Vegas total and spread drive each team's implied points. Pace
and pass rate come from a team's own history, shrunk toward the league mean.
Defensive adjustments are pulled halfway to neutral because they are noisy.

**Usage.** Target, carry and dropback shares are estimated from recency-weighted
history, blended with depth-chart priors by a role-confidence weight, then
sharpened so the projected within-team share curve matches the historical one
(the WR1 on an average team takes 23.7% of targets; averaging per-game shares
understates that, because the leader is the max of a noisy set, not the max of
their means). Quarterback shares are re-derived from *who starts* rather than
season averages, which otherwise invent timeshares that will not happen.

**Efficiency.** Per-opportunity rates are shrunk toward positional priors with
explicit prior sample sizes — 260 attempts for yards per attempt, 55 targets for
catch rate, and so on.

**Market blend.** Bottom-up projections are blended with an isotonic fit of
points against the player's rank *within his own position* on the consensus
board. Fitting across positions would be wrong: a quarterback going 107th
overall is not the 107th-best scorer, he is priced for scarcity.

**Simulation.** Per game: margin ~ N(spread, 12.73) and total ~ N(total, 13.19);
plays and pass rate respond to the *realised* margin (β = −0.0043 per point,
estimated with team fixed effects); usage is Dirichlet-drawn around projected
shares with inactive players zeroed first, so injuries genuinely redistribute
volume; yardage uses shifted gammas fit on play-by-play, so a full game's
production is one draw rather than a loop; touchdowns come from team points and
are allocated across the roster.

Each simulated *season* also draws persistent shocks — a role multiplier per
player, an efficiency multiplier, a team-strength surprise (σ ≈ 2.6 points per
game) and a pass/rush touchdown split. Without them the leaderboard comes out
far too flat: week-to-week noise alone never lets a WR2 seize the WR1 job for a
year.

Correlation is structural rather than bolted on. Teammates share one points
draw, one play count and one target pie; a quarterback's passing yards *are* the
sum of his receivers' receiving yards. Stacks, game-script effects and the
negative correlation between teammates all emerge on their own.

### Does it work?

`make validate` simulates the season and compares the *rank-for-rank* finish
curve against 2023–2025 actuals. Rank-for-rank matters: nobody knows in advance
who finishes WR3, so an ex-ante projection can never be compared to an ex-post
leaderboard directly.

Current mean absolute deviation is **7.3%** across 24 checkpoints:

| | rank 1 | rank 6 | rank 12 | rank 24 |
| --- | --- | --- | --- | --- |
| QB | −14% | −8% | −6% | +19% |
| RB | +2% | ±0% | −1% | −10% |
| WR | +9% | +13% | +9% | ±0% |
| TE | +12% | +3% | +2% | −7% |

Running backs land within a couple of percent across the startable range. Two
known weaknesses: elite quarterback seasons come in light, and the QB24 line is
too generous because the model has no concept of a starter being benched — a
real thing that separates a replacement-level quarterback's actual season from
his projected one.

### Recalibrating

```bash
make calibrate      # refits every constant, writes data/artifacts/calibration.json
```

That file overrides the committed defaults at import time, so the engine
improves as seasons land without a code change. `--dry-run` prints the fit and
its change against the current values without writing.

---

## Data sources

| Source | Used for |
| --- | --- |
| [nflverse](https://github.com/nflverse/nflverse-data) | play-by-play, weekly stats, rosters, depth charts, snap counts, schedules with closing lines |
| [DynastyProcess](https://github.com/dynastyprocess/data) mirror of FantasyPros | consensus ranks with dispersion, cross-site player ids |
| FantasyFootballCalculator | mock-draft ADP (optional) |
| Sleeper | player metadata, injury status (optional) |
| Kalshi / Polymarket | live contract quotes and order books (optional) |

Everything is cached to parquet under `data/cache`, so after one warm-up the app
works offline. If a source is unreachable the cache is served stale rather than
failing — a draft room should not go dark because GitHub had a bad minute.

Drop a CSV at `data/user/adp.csv` (columns `player`, `pos`, `adp`, optionally
`adp_sd`) to override the ADP source with your own league's board.

---

## Scheduling

The built-in scheduler runs at 00:00 and 12:00 local and is controlled from the
dashboard. It refreshes source data, rebuilds projections, and snapshots market
books. History of every run is visible there too.

To let the OS drive it instead:

```cron
0 0,12 * * * cd /path/to/fantasy && .venv/bin/gridiron refresh && .venv/bin/gridiron build --sims 20000
```

On Windows, Task Scheduler running `run.bat` with the same two triggers.

---

## Command line

```
gridiron serve [--port 8000] [--demo]   start the web app
gridiron refresh                        pull the latest data
gridiron build --sims 20000             rebuild projections and simulations
gridiron validate                       check the model against history
gridiron calibrate                      refit simulation constants
gridiron board --position RB            print the draft board in the terminal
gridiron doctor                         check GPU, data sources, venues, trading mode
```

`./update.sh` updates an existing install in place without re-downloading data.

---

## Layout

```
src/gridiron/
  config.py          paths, backend detection, VRAM-aware chunking
  scoring.py         league settings and fantasy point arithmetic
  data/              nflverse + market loaders, parquet cache
  features/          team context, player usage and efficiency profiles
  models/            projections, availability, kickers and defences
  sim/               Monte Carlo engine, array backend, league simulation
  draft/             VBD, tiers, availability curve, draft simulator
  quant/             odds, devig, Stern model, Kelly, calibration, microstructure
  exchange/          Kalshi, Polymarket, paper broker, risk guard, tick store
  analysis/          game predictions and breakdowns
  api/               FastAPI server
  cli.py             command line
web/                 single-page frontend (no build step, no CDN)
  assets/guide-data.js   the stats reference content
  assets/guide.js        the Stats Guide view
scripts/             calibration and validation
tests/               144 tests
```

## Tests

```bash
make test
```

The API tests run against the real stack with a small simulation budget rather
than mocks, and skip rather than fail when data is unavailable.

Backend tests come in two halves. Contract tests run anywhere and prove
`CuPyBackend` covers the whole interface with matching signatures — including a
guard that it still inherits the shared maths rather than drifting into a second
implementation. Parity tests then check distributional behaviour against every
backend importable on the machine, so the CuPy path is exercised automatically
on a box that has a GPU.

---

## Limits worth knowing

- Preseason projections cannot see a training-camp job change until it reaches
  the consensus board; the blend leans on the market for exactly this reason.
- The model has no concept of benching, which is why replacement-level
  quarterbacks project too well.
- Weather and referee assignments are ignored.
- Kicker and defence models are deliberately simple, because their week-to-week
  outcomes are close to unforecastable anyway.
- Market edges are computed against a model that has *not* been validated
  out-of-sample against market prices — treat them as a starting point for your
  own research, not a signal to size up on.
