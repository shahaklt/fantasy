// Draft Room, Games, Markets and Live Tape views.
import { $, api, barChart, bounds, distBar, DRAFT_SIMS, el, fmt, gauge, heatCell, histogram,
        kv, led, metric, panel, posTag, SIMS, sparkline, table, toast } from './ui.js';
import { openDrawer, store } from './views1.js';

// ---------------------------------------------------------------- DRAFT ROOM
export async function draft(root) {
  // Named rather than inline so the phone breakpoint can collapse it; an
  // inline grid-template outranks any media query.
  const wrap = el('div', { class: 'grid split' });
  root.replaceChildren(wrap);
  const leftCol = el('div', {});
  const rightCol = el('div', { class: 'grid', style: 'gap:14px;align-content:start' });
  wrap.append(leftCol, rightCol);

  const state = { position: 'ALL', search: '' };

  async function render() {
    const [board, st, scarcity] = await Promise.all([
      api.get(`/api/draft/board?limit=400${state.position !== 'ALL' ? `&position=${state.position}` : ''}`),
      api.get('/api/draft/state'),
      api.get('/api/draft/scarcity'),
    ]);

    let players = board.players;
    if (state.search) {
      const q = state.search.toLowerCase();
      players = players.filter((p) => p.player_name.toLowerCase().includes(q));
    }
    const nextPicks = board.my_next_picks || [];
    const availCol = nextPicks[1] ? `avail_at_${nextPicks[1]}` : null;

    const vb = bounds(players, 'vorp');
    const distLo = Math.min(...players.map((r) => r.p5 ?? 0));
    const distHi = Math.max(...players.map((r) => r.p95 ?? 1));
    const cols = [
      { key: 'player_name', label: 'Player', cls: 'name',
        fmt: (v, r) => el('span', {}, posTag(r.position), ' ', v) },
      { key: 'team', label: 'Tm', cls: 'dim' },
      { key: 'tier', label: 'Tier', cls: 'num', fmt: (v) => el('span', { class: 'tier' }, v ?? '—') },
      { key: 'proj_points', label: 'Proj', cls: 'num', fmt: (v) => fmt.n(v, 0) },
      { key: 'p50', label: 'Range', sortable: false,
        fmt: (v, r) => distBar(r.p5, r.p50 ?? r.proj_points, r.p95, distLo, distHi) },
      { key: 'vorp', label: 'VORP', cls: 'num',
        cellAttrs: (v) => heatCell(v, vb[0], vb[1]),
        fmt: (v) => el('span', { class: v > 0 ? 'good' : 'dim' }, fmt.n(v, 0)) },
      { key: 'auction_value', label: '$', cls: 'num', fmt: (v) => fmt.money(v) },
      { key: 'adp', label: 'ADP', cls: 'num dim', fmt: (v) => fmt.n(v, 1) },
      availCol ? { key: availCol, label: `@${nextPicks[1]}`, cls: 'num',
        cellAttrs: (v) => heatCell(v ?? 0, 0, 1),
        fmt: (v) => el('span', { class: v > 0.66 ? 'good' : v > 0.33 ? 'warn' : 'bad' }, fmt.pct(v, 0)) } : null,
      { key: 'player_id', label: '', sortable: false, fmt: (v) => el('button', {
          class: 'btn sm', onclick: async (e) => {
            e.stopPropagation();
            try { await api.post('/api/draft/pick', { player_id: v }); render(); }
            catch (err) { toast(String(err), true); }
          } }, 'Draft') },
    ].filter(Boolean);

    const onClock = board.on_the_clock === board.my_slot;
    const header = el('div', { class: 'grid split-head' },
      panel('Draft state',
        el('div', { class: 'grid tiles' },
          gauge('overall pick', `#${board.pick}`,
            `round ${Math.ceil(board.pick / (store.league?.teams || 12))}`),
          gauge('on the clock', `T${board.on_the_clock}`,
            onClock ? '▸ that is you' : `you pick at #${nextPicks[0] ?? '—'}`),
          gauge('your next', nextPicks.slice(0, 3).join(' · ') || '—',
            `${board.drafted_count} off the board`)),
        { actions: [led(onClock ? 'busy' : 'on')] }),
      panel('Actions',
        el('div', { style: 'display:flex;flex-direction:column;gap:6px;min-width:180px' },
          el('button', { class: 'btn primary', onclick: (e) => recommend(e, rightCol) }, 'Recommend my pick'),
          el('div', { style: 'display:flex;gap:6px' },
            el('button', { class: 'btn sm ghost', style: 'flex:1',
              onclick: async () => { await api.post('/api/draft/undo'); render(); } }, 'Undo'),
            el('button', { class: 'btn sm ghost', style: 'flex:1',
              onclick: async () => {
                if (confirm('Reset the draft?')) { await api.post('/api/draft/reset', {}); render(); }
              } }, 'Reset')),
          // A draft that already happened on ESPN should not be re-entered by hand.
          el('button', {
            class: 'btn sm ghost',
            onclick: async (e) => {
              e.target.disabled = true; e.target.textContent = 'importing';
              try {
                const r = await api.post('/api/espn/sync', {
                  settings: false, roster: false, schedule: false, draft: true });
                const d = (r.imported || {}).draft || {};
                toast(d.recorded
                  ? `imported ${d.recorded} picks from ESPN`
                  : (d.note || 'nothing to import'), !d.recorded);
                render();
              } catch (err) { toast(String(err), true); }
              e.target.disabled = false; e.target.textContent = 'Import ESPN draft';
            },
          }, 'Import ESPN draft'))));

    const controls = el('div', { class: 'controls' },
      el('div', { class: 'seg' }, ['ALL', 'QB', 'RB', 'WR', 'TE', 'K', 'DST'].map((p) =>
        el('button', { class: p === state.position ? 'active' : '',
          onclick: () => { state.position = p; render(); } }, p))),
      el('input', { type: 'search', placeholder: 'Search…', value: state.search,
        oninput: (e) => { state.search = e.target.value; clearTimeout(render._t);
          render._t = setTimeout(render, 200); } }));

    leftCol.replaceChildren(header, controls,
      panel('Best available', table(players, cols, {
        sortKey: 'vorp', onRow: (r) => import('./views1.js').then((m) => m.playerDrawer(r.player_id)),
        empty: 'Nobody left matching that filter' }),
        { flush: true, meta: `${players.length} available` }));

    // ---- right column: scarcity, my roster, recent picks
    const myRoster = (st.rosters?.[String(board.my_slot)] || []);
    rightCol.replaceChildren(
      panel('Positional scarcity', el('div', {},
        barChart(scarcity.map((s) => ({ label: `${s.position} · ${s.available} left`, value: s.drop_12,
          color: { QB: '#a78bfa', RB: '#4ade80', WR: '#60a5fa', TE: '#fbbf24', K: '#f472b6', DST: '#94a3b8' }[s.position] })),
          { fmt: (v) => fmt.n(v, 0) }),
        el('div', { class: 'note', style: 'margin-top:8px' },
          'Points you lose by waiting 12 more picks at each position.'))),
      panel('My roster', el('div', {},
        myRoster.length ? myRoster.map((p) => el('div', { class: 'roster-slot' },
          el('span', {}, posTag(p.position || '—'), ' ', p.player_name || p.player_id),
          el('span', { class: 'num' }, fmt.n(p.proj_points, 0))))
          : el('div', { class: 'note' }, 'No picks yet.')),
        { meta: `${myRoster.length} players` }),
      panel('Recent picks', el('div', {},
        (st.picks || []).slice(-12).reverse().map((p) => el('div', { class: 'kv' },
          el('span', { class: 'dim' }, `#${p.pick} · T${p.team_slot}`),
          el('span', {}, p.player?.player_name || p.player?.player_id || '—')))
        || el('div', { class: 'note' }, 'Draft has not started.'))));
  }

  async function recommend(evt, container) {
    const btn = evt.target;
    btn.disabled = true; btn.textContent = 'simulating…';
    try {
      const r = await api.post('/api/draft/recommend', { n_sims: DRAFT_SIMS, top_k: 12 });
      const best = r.recommendations[0];
      const card = el('div', { class: 'card' },
        el('h3', {}, `Best pick at #${r.pick}`),
        best ? el('div', { style: 'margin-bottom:10px' },
          el('div', { style: 'font-size:18px;font-weight:600' }, posTag(best.position), ' ', best.player_name),
          el('div', { class: 'note' },
            `Finishing this draft ${r.recommendations.length} different ways, taking him leaves the strongest `
            + `starting lineup: ${fmt.n(best.roster_value, 0)} projected points.`)) : null,
        table(r.recommendations, [
          { key: 'player_name', label: 'Player', fmt: (v, row) => el('span', {}, posTag(row.position), ' ', v) },
          { key: 'roster_value', label: 'Roster', cls: 'num', fmt: (v) => fmt.n(v, 0) },
          { key: 'value_vs_best', label: 'vs best', cls: 'num',
            fmt: (v) => el('span', { class: v >= -1 ? 'good' : v > -25 ? 'warn' : 'bad' }, fmt.signed(v, 0)) },
          { key: 'adp', label: 'ADP', cls: 'num dim', fmt: (v) => fmt.n(v, 1) },
        ], { sortKey: 'roster_value' }));
      container.prepend(card);
    } catch (err) { toast(String(err), true); }
    btn.disabled = false; btn.textContent = 'Recommend my pick';
  }

  await render();
}

// -------------------------------------------------------------------- GAMES
export async function games(root) {
  const controls = el('div', { class: 'controls' });
  const body = el('div', {});
  root.replaceChildren(controls, body);

  const weekSel = el('select', {
    onchange: (e) => { store.week = Number(e.target.value); load(); } },
    Array.from({ length: 18 }, (_, i) => el('option', { value: i + 1,
      selected: i + 1 === store.week ? 'selected' : null }, `Week ${i + 1}`)));
  controls.append(weekSel, el('span', { class: 'note' },
    'Each game is simulated as one coherent contest — one margin, one total, every player inside it.'));

  async function load() {
    body.replaceChildren(el('div', { class: 'empty' }, el('span', { class: 'loading' }), ' simulating the week'));
    const data = await api.get(`/api/games?week=${store.week}&n_sims=${SIMS}`);
    const games = data.games;
    const rows = games.map((g) => {
      const edge = Math.abs(g.spread_edge) > 1.5 || Math.abs(g.total_edge) > 1.5;
      const favTeam = g.home_win_prob > g.away_win_prob ? g.home_team : g.away_team;
      const favProb = Math.max(g.home_win_prob, g.away_win_prob);
      return el('div', { class: 'game-row', onclick: () => gameDrawer(g.game_id, store.week) },
        el('div', {},
          el('div', { class: 'matchup' },
            el('b', {}, g.away_team), el('span', { class: 'at' }, '  at  '), el('b', {}, g.home_team),
            edge ? el('span', { class: 'pill busy', style: 'margin-left:8px' }, 'edge') : null),
          el('div', { class: 'note', style: 'font-family:var(--mono);font-size:10px;margin-top:3px' },
            `line ${g.market_spread > 0 ? g.home_team : g.away_team} `
            + `-${fmt.n(Math.abs(g.market_spread), 1)} · o/u ${fmt.n(g.market_total, 1)}`
            + `   model ${fmt.signed(g.spread_edge, 1)} / ${fmt.signed(g.total_edge, 1)}`)),
        el('div', { style: 'text-align:right' },
          el('div', { class: 'score' },
            `${fmt.n(g.proj_away_score, 1)}–${fmt.n(g.proj_home_score, 1)}`),
          el('div', { class: 'note', style: 'font-family:var(--mono);font-size:10px;margin-top:3px' },
            `${favTeam} ${fmt.pct(favProb, 0)} · over ${fmt.pct(g.over_prob, 0)}`)));
    });

    const totals = games.map((g) => g.proj_total);
    body.replaceChildren(
      el('div', { class: 'grid g4', style: 'margin-bottom:12px' },
        panel('Slate', gauge('games', String(games.length), `week ${store.week}`)),
        panel('Avg total', gauge('points', fmt.n(totals.reduce((a, b) => a + b, 0) / (totals.length || 1), 1),
          `high ${fmt.n(Math.max(...totals), 1)} · low ${fmt.n(Math.min(...totals), 1)}`)),
        (() => {
          const top = games.slice().sort((a, b) => b.proj_total - a.proj_total)[0];
          return panel('Highest total', gauge('shootout',
            top ? `${top.away_team}@${top.home_team}` : '—',
            top ? `${fmt.n(top.proj_total, 1)} pts — start everyone in it` : ''));
        })(),
        (() => {
          const e = games.slice().sort((a, b) =>
            Math.abs(b.spread_edge) - Math.abs(a.spread_edge))[0];
          return panel('Biggest edge', gauge('vs market',
            fmt.signed(e?.spread_edge || 0, 1),
            e ? `${e.away_team}@${e.home_team} · points of spread` : ''));
        })()),
      panel('Matchups', el('div', {}, rows),
        { flush: true, meta: 'click a game for the full breakdown' }));
  }

  await load();
}

async function gameDrawer(gameId, week) {
  openDrawer(el('div', { class: 'empty' }, el('span', { class: 'loading' }), ' building breakdown'));
  try {
    const d = await api.get(`/api/games/${gameId}?week=${week}&n_sims=${SIMS}`);
    const s = d.summary;
    const teamCard = (b, label) => panel(`${label} · ${b.team}`, el('div', {},
      kv('Fantasy points', fmt.n(b.totals.fantasy_points, 1)),
      kv('Passing yards', fmt.n(b.totals.passing_yards, 0)),
      kv('Rushing yards', fmt.n(b.totals.rushing_yards, 0)),
      kv('Touchdowns', fmt.n(b.totals.total_tds, 2)),
      kv('Targets / carries', `${fmt.n(b.totals.targets, 1)} / ${fmt.n(b.totals.carries, 1)}`),
      el('div', { style: 'margin-top:10px' },
        barChart(b.players.slice(0, 8).map((p) => ({ label: p.player_name, value: p.proj_points,
          color: { QB: '#a78bfa', RB: '#4ade80', WR: '#60a5fa', TE: '#fbbf24', K: '#f472b6', DST: '#94a3b8' }[p.position] })),
          { fmt: (v) => fmt.n(v, 1) }))));

    openDrawer(el('div', {},
      el('div', { style: 'margin-bottom:14px' },
        el('h2', { style: 'margin:0 0 3px;font:600 20px/1.2 var(--sans)' },
          `${s.away_team} at ${s.home_team}`),
        el('div', { class: 'note' },
          `Week ${s.week} · market ${fmt.signed(s.market_spread, 1)} / ${fmt.n(s.market_total, 1)}`)),
      el('div', { class: 'grid g4', style: 'margin-bottom:14px' },
        panel(null, gauge('Predicted', `${fmt.n(s.proj_away_score, 1)}–${fmt.n(s.proj_home_score, 1)}`, 'away – home')),
        panel(null, gauge('Total', fmt.n(s.proj_total, 1), `market ${fmt.n(s.market_total, 1)} · over ${fmt.pct(s.over_prob, 0)}`)),
        panel(null, gauge('Margin', fmt.signed(s.proj_margin, 1), `±${fmt.n(s.margin_sd, 1)} sd`)),
        panel(null, gauge('Home win', fmt.pct(s.home_win_prob, 1), `cover ${fmt.pct(s.home_cover_prob, 0)}`))),
      el('div', { class: 'grid g2', style: 'margin-bottom:14px' },
        panel('Simulated total', histogram(d.distribution.total, { marker: s.market_total }),
          { meta: `sd ${fmt.n(s.total_sd, 1)}` }),
        panel('Simulated margin', histogram(d.distribution.margin,
          { marker: s.market_spread, color: '#7dd3fc' }), { meta: `sd ${fmt.n(s.margin_sd, 1)}` })),
      el('div', { class: 'grid g2', style: 'margin-bottom:14px' },
        teamCard(d.away_breakdown, 'Away'), teamCard(d.home_breakdown, 'Home')),
      el('div', { style: 'margin-bottom:14px' }, panel('Alternate lines',
        el('div', { class: 'grid g2' },
          table(d.alt_lines.spreads, [
            { key: 'line', label: 'Spread', cls: 'num', fmt: (v) => fmt.signed(v, 1) },
            { key: 'home_cover', label: `${s.home_team} covers`, cls: 'num', fmt: (v) => fmt.pct(v, 1) }], {}),
          table(d.alt_lines.totals, [
            { key: 'line', label: 'Total', cls: 'num', fmt: (v) => fmt.n(v, 1) },
            { key: 'over', label: 'Over', cls: 'num', fmt: (v) => fmt.pct(v, 1) }], {})))),
      panel('Closed-form cross-check · Stern model',
        el('div', {},
          kv('Home win probability', fmt.pct(d.analytic.home_win_prob, 1)),
          kv('Implied volatility', fmt.n(d.analytic.implied_volatility, 2)),
          el('div', { class: 'note', style: 'margin-top:8px' },
            'The Brownian-motion model prices the same game analytically from the spread and '
            + 'total alone. A large gap against the simulation usually means a lineup or usage '
            + 'assumption is doing the work.')))));
  } catch (err) {
    openDrawer(el('div', { class: 'empty bad' }, String(err)));
  }
}

// ------------------------------------------------------------------ MARKETS
export async function markets(root) {
  const wrap = el('div', {});
  root.replaceChildren(wrap);

  async function render() {
    wrap.replaceChildren(el('div', { class: 'empty' }, el('span', { class: 'loading' }), ' contacting venues'));
    const [m, portfolio] = await Promise.all([api.get('/api/markets'), api.get('/api/portfolio')]);

    const venueCards = el('div', { class: 'grid g3' },
      m.venues.map((v) => panel(v.venue,
        el('div', {},
          kv('authenticated', v.authenticated ? 'yes' : 'read-only'),
          kv('markets', v.reachable ? 'live' : 'unavailable'),
          v.reachable ? null : el('div', { class: 'note', style: 'margin-top:8px' }, v.detail)),
        { actions: [led(v.reachable ? 'on' : 'off')] })),
      panel('Paper account', el('div', {},
        kv('Cash', fmt.money(portfolio.paper.cash)),
        kv('Equity', fmt.money(portfolio.paper.equity)),
        kv('Open positions', portfolio.paper.positions.length),
        kv('mode', portfolio.mode))));

    const scanBtn = el('button', { class: 'btn primary', onclick: async (e) => {
      e.target.disabled = true; e.target.textContent = 'scanning…';
      try {
        const r = await api.post('/api/markets/signals', { week: store.week, min_edge: 0.02, n_sims: SIMS });
        signalBox.replaceChildren(signalTable(r));
        toast(`${r.signals.length} edges across ${r.markets_scanned} markets`);
      } catch (err) { toast(String(err), true); }
      e.target.disabled = false; e.target.textContent = 'Scan for edges';
    } }, 'Scan for edges');

    const signalBox = el('div', { style: 'margin-top:14px' },
      el('div', { class: 'note' }, 'Run a scan to price every listed NFL contract against the simulation.'));

    const tradesCard = el('div', { style: 'margin-top:14px' }, panel('Trade log',
      table(portfolio.trades, [
        { key: 'ts', label: 'Time', fmt: (v) => fmt.time(v) },
        { key: 'mode', label: 'Mode', fmt: (v) => el('span', { class: v === 'live' ? 'warn' : 'dim' }, v) },
        { key: 'venue', label: 'Venue', cls: 'dim' },
        { key: 'market_id', label: 'Market' },
        { key: 'side', label: 'Side' },
        { key: 'quantity', label: 'Qty', cls: 'num', fmt: (v) => fmt.i(v) },
        { key: 'price', label: 'Price', cls: 'num', fmt: (v) => fmt.n(v, 3) },
        { key: 'status', label: 'Status', cls: 'dim' },
      ], { empty: 'No trades yet' }), { flush: true }));

    const sentimentBox = el('div', { style: 'margin-top:14px' },
      el('div', { class: 'empty' }, el('span', { class: 'loading' }), ' reading public prices'));

    wrap.replaceChildren(venueCards, sentimentBox,
      el('div', { style: 'margin-top:14px' },
        panel('Model vs market', signalBox, { actions: [scanBtn] })),
      tradesCard);

    // Sentiment loads on its own so a slow venue never blocks the rest of the
    // page, and it needs neither credentials nor a finished simulation.
    loadSentiment(sentimentBox);
  }

  async function loadSentiment(box) {
    let d;
    try {
      d = await api.get('/api/markets/sentiment');
    } catch (err) {
      box.replaceChildren(panel('Public sentiment',
        el('div', { class: 'note bad' }, String(err))));
      return;
    }

    if (!d.consensus.length) {
      const down = (d.venues || []).filter((v) => !v.reachable);
      box.replaceChildren(panel('Public sentiment', el('div', { class: 'note' },
        down.length
          ? `No venue is reachable right now. ${down.map((v) => `${v.venue}: ${v.detail}`).join(' · ')}`
          : 'Both venues answered but listed no NFL contracts — normal in the offseason.'),
        { actions: [led('off')] }));
      return;
    }

    const stats = el('div', { class: 'grid g4', style: 'margin-bottom:12px' },
      panel('Questions priced', gauge('contracts', fmt.i(d.question_count),
        `${fmt.i(d.quote_count)} quotes across ${d.venues.filter((v) => v.reachable).length} venue(s)`)),
      panel('Money at stake', gauge('volume', fmt.money(d.total_volume), 'across all listed NFL contracts')),
      panel('Both venues', gauge('overlap', fmt.i(d.both_venues),
        'questions quoted on Kalshi and Polymarket')),
      panel('Widest split', d.widest_disagreement
        ? gauge('disagreement', fmt.pct(d.widest_disagreement.disagreement, 1),
            d.widest_disagreement.label)
        : gauge('disagreement', '—', 'no question is quoted on both')));

    const rows = table(d.consensus, [
      { key: 'label', label: 'Question', cls: 'name' },
      { key: 'kind', label: 'Type', cls: 'dim' },
      { key: 'probability', label: 'Crowd', cls: 'num',
        fmt: (v) => el('b', {}, fmt.pct(v, 1)) },
      { key: 'probability', label: 'Implied odds', cls: 'num dim',
        fmt: (v) => (v > 0 && v < 1
          ? (v >= 0.5 ? `-${Math.round(100 * v / (1 - v))}` : `+${Math.round(100 * (1 - v) / v)}`)
          : '—') },
      { key: 'volume', label: 'Volume', cls: 'num', fmt: (v) => fmt.money(v) },
      { key: 'venues', label: 'Venues', cls: 'dim', fmt: (v) => v.join(' · ') },
      { key: 'overround', label: 'Vig', cls: 'num dim',
        fmt: (v) => (v == null ? '—' : fmt.pct(v, 1)) },
      { key: 'disagreement', label: 'Split', cls: 'num',
        fmt: (v) => (v == null ? '—'
          : el('span', { class: v > 0.05 ? 'warn' : 'dim' }, fmt.pct(v, 1))) },
    ], { sortKey: 'volume', sortDesc: true, empty: 'nothing priced' });

    box.replaceChildren(stats, panel('Public sentiment', rows, {
      flush: true,
      meta: `read-only · fetched ${d.fetched_at}`,
      actions: [led('on'), el('button', { class: 'btn ghost sm',
        onclick: () => loadSentiment(box) }, 'Refresh')],
    }), el('div', { class: 'note', style: 'margin-top:8px' },
      'Public prices, no account involved. "Crowd" is the volume-and-spread weighted '
      + 'consensus; where both sides of a game are quoted the house edge is removed '
      + 'exactly, and "Vig" is how much there was. "Split" is how far the two venues '
      + 'disagree — a wide split is itself a signal.'));
  }

  function signalTable(r) {
    if (!r.signals.length) {
      return el('div', { class: 'note' },
        `Scanned ${r.markets_scanned} markets and found no edge above the threshold. `
        + (r.markets_scanned === 0 ? 'No venue returned any markets — check connectivity.' : ''));
    }
    return table(r.signals, [
      { key: 'title', label: 'Market' },
      { key: 'venue', label: 'Venue', cls: 'dim' },
      { key: 'side', label: 'Side' },
      { key: 'market_prob', label: 'Price', cls: 'num', fmt: (v) => fmt.n(v, 3) },
      { key: 'model_prob', label: 'Model', cls: 'num', fmt: (v) => fmt.n(v, 3) },
      { key: 'blended_prob', label: 'Blended', cls: 'num', fmt: (v) => fmt.n(v, 3) },
      { key: 'edge', label: 'Edge', cls: 'num', fmt: (v) => el('span', { class: v > 0.04 ? 'good' : 'warn' }, fmt.pct(v, 1)) },
      { key: 'kelly_fraction', label: 'Kelly', cls: 'num', fmt: (v) => fmt.pct(v, 2) },
      { key: 'suggested_stake', label: 'Stake', cls: 'num', fmt: (v) => fmt.money(v) },
      { key: 'market_id', label: '', sortable: false, fmt: (v, row) => el('button', {
          class: 'btn sm', onclick: async () => {
            const qty = Number(prompt(`Contracts to buy at ${fmt.n(row.price, 3)}?`,
              String(Math.max(1, Math.floor(row.suggested_stake / Math.max(row.price, 0.01))))));
            if (!qty) return;
            try {
              const res = await api.post('/api/trade', { venue: row.venue, market_id: v,
                side: row.side, quantity: qty, price: row.price, model_prob: row.model_prob,
                confirm: false, live: false });
              toast(res.submitted ? `Paper order filled: ${qty} @ ${fmt.n(row.price, 3)}` : res.reason, !res.submitted);
              render();
            } catch (err) { toast(String(err), true); }
          } }, 'Paper buy') },
    ], { sortKey: 'edge' });
  }

  await render();
}

// ---------------------------------------------------------------- LIVE TAPE
export async function live(root) {
  const controls = el('div', { class: 'controls' });
  const grid = el('div', { class: 'grid g3' });
  const status = el('span', { class: 'note' });
  root.replaceChildren(controls, status, grid);

  const startBtn = el('button', { class: 'btn primary', onclick: async () => {
    try {
      const r = await api.post('/api/markets/poll/start', { interval: 5, limit: 40 });
      toast(`Watching ${r.markets} markets every ${r.interval}s`);
      connect();
    } catch (err) { toast(String(err), true); }
  } }, 'Start tape');
  const stopBtn = el('button', { class: 'btn ghost', onclick: async () => {
    await api.post('/api/markets/poll/stop'); toast('Tape stopped'); if (ws) ws.close();
  } }, 'Stop');
  controls.append(startBtn, stopBtn, el('span', { class: 'note' },
    'Streams live order books and derives micro-price, book imbalance and order-flow imbalance — '
    + 'the mechanics behind sentiment moving before the last-traded price does.'));

  let ws = null;
  function connect() {
    if (ws) ws.close();
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws/markets`);
    ws.onmessage = (evt) => {
      const data = JSON.parse(evt.data);
      status.textContent = `${data.markets.length} contracts · updated ${new Date().toLocaleTimeString()}`
        + (data.polling ? '' : ' · poller stopped');
      grid.replaceChildren(...data.markets.map(tile));
    };
    ws.onclose = () => { status.textContent += ' · stream closed'; };
  }

  function tile(m) {
    const hist = (m.history || []).map((h) => h.mid).filter((v) => v != null);
    const drift = hist.length > 1 ? hist[hist.length - 1] - hist[0] : 0;
    const skew = m.imbalance || 0;
    return panel(m.title || m.market_id,
      el('div', {},
        el('div', { style: 'display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px' },
          el('span', { class: 'score' }, m.mid != null ? `${fmt.n(m.mid * 100, 1)}¢` : '—'),
          el('span', { class: drift >= 0 ? 'good' : 'bad', style: 'font:11px/1 var(--mono)' },
            `${drift >= 0 ? '▲' : '▼'} ${fmt.n(Math.abs(drift) * 100, 2)}¢`)),
        sparkline(hist, { height: 40, color: drift >= 0 ? '#4ade80' : '#fb7185' }),
        el('div', { style: 'margin-top:8px' },
          metric('micro-price', m.micro_price != null ? `${fmt.n(m.micro_price * 100, 2)}¢` : '—',
            m.micro_price > m.mid),
          metric('spread', m.spread != null ? `${fmt.n(m.spread * 100, 1)}¢` : '—'),
          metric('book imbalance', fmt.n(skew, 3)),
          el('div', { class: 'gauge-track', style: 'margin:2px 0 8px' },
            el('div', { class: 'gauge-fill',
              style: `width:${Math.abs(skew) * 50}%;margin-left:${skew < 0 ? 50 - Math.abs(skew) * 50 : 50}%;`
                + `background:${skew >= 0 ? 'var(--good)' : 'var(--bad)'}` })),
          metric('momentum z', fmt.n(m.momentum, 2), Math.abs(m.momentum) > 2),
          metric('realised vol', fmt.n((m.realised_vol || 0) * 100, 3)))),
      { meta: m.venue });
  }

  try {
    const s = await api.get('/api/status');
    if (s.polling) connect();
  } catch { /* status is optional here */ }
}
