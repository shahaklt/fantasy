// Draft Room, Games, Markets and Live Tape views.
import { $, api, barChart, el, fmt, histogram, kv, posTag, sparkline, stat, table, toast } from './ui.js';
import { openDrawer, store } from './views1.js';

// ---------------------------------------------------------------- DRAFT ROOM
export async function draft(root) {
  const wrap = el('div', { class: 'grid', style: 'grid-template-columns:minmax(0,2.1fr) minmax(300px,1fr);gap:14px' });
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

    const cols = [
      { key: 'player_name', label: 'Player', fmt: (v, r) => el('span', {}, posTag(r.position), ' ', v) },
      { key: 'team', label: 'Tm', cls: 'dim' },
      { key: 'tier', label: 'Tier', cls: 'num', fmt: (v) => el('span', { class: 'tier' }, v ?? '—') },
      { key: 'proj_points', label: 'Proj', cls: 'num', fmt: (v) => fmt.n(v, 0) },
      { key: 'vorp', label: 'VORP', cls: 'num', fmt: (v) => el('span', { class: v > 0 ? 'good' : 'dim' }, fmt.n(v, 0)) },
      { key: 'auction_value', label: '$', cls: 'num', fmt: (v) => fmt.money(v) },
      { key: 'adp', label: 'ADP', cls: 'num dim', fmt: (v) => fmt.n(v, 1) },
      availCol ? { key: availCol, label: `@${nextPicks[1]}`, cls: 'num',
        fmt: (v) => el('span', { class: v > 0.66 ? 'good' : v > 0.33 ? 'warn' : 'bad' }, fmt.pct(v, 0)) } : null,
      { key: 'player_id', label: '', sortable: false, fmt: (v) => el('button', {
          class: 'btn sm', onclick: async (e) => {
            e.stopPropagation();
            try { await api.post('/api/draft/pick', { player_id: v }); render(); }
            catch (err) { toast(String(err), true); }
          } }, 'Draft') },
    ].filter(Boolean);

    const header = el('div', { class: 'card', style: 'margin-bottom:14px' },
      el('div', { class: 'card-head' },
        el('div', { style: 'display:flex;gap:22px;align-items:center;flex-wrap:wrap' },
          stat('Pick', `#${board.pick}`, `round ${Math.ceil(board.pick / (store.league?.teams || 12))}`),
          stat('On the clock', `Team ${board.on_the_clock}`,
            board.on_the_clock === board.my_slot ? 'that is you' : `you pick at #${nextPicks[0] ?? '—'}`),
          stat('Your next picks', nextPicks.slice(0, 4).join(', ') || '—', `${board.drafted_count} players off the board`)),
        el('div', { style: 'display:flex;gap:6px' },
          el('button', { class: 'btn sm ghost', onclick: async () => { await api.post('/api/draft/undo'); render(); } }, 'Undo'),
          el('button', { class: 'btn sm ghost', onclick: async () => {
            if (confirm('Reset the draft?')) { await api.post('/api/draft/reset', {}); render(); } } }, 'Reset'),
          el('button', { class: 'btn primary sm', onclick: (e) => recommend(e, rightCol) }, 'Recommend my pick'))));

    const controls = el('div', { class: 'controls' },
      el('div', { class: 'seg' }, ['ALL', 'QB', 'RB', 'WR', 'TE', 'K', 'DST'].map((p) =>
        el('button', { class: p === state.position ? 'active' : '',
          onclick: () => { state.position = p; render(); } }, p))),
      el('input', { type: 'search', placeholder: 'Search…', value: state.search,
        oninput: (e) => { state.search = e.target.value; clearTimeout(render._t);
          render._t = setTimeout(render, 200); } }));

    leftCol.replaceChildren(header, controls,
      el('div', { class: 'card pad0' }, table(players, cols, {
        sortKey: 'vorp', onRow: (r) => import('./views1.js').then((m) => m.playerDrawer(r.player_id)),
        empty: 'Nobody left matching that filter' })));

    // ---- right column: scarcity, my roster, recent picks
    const myRoster = (st.rosters?.[String(board.my_slot)] || []);
    rightCol.replaceChildren(
      el('div', { class: 'card' },
        el('h3', {}, 'Positional scarcity'),
        barChart(scarcity.map((s) => ({ label: `${s.position} · ${s.available} left`, value: s.drop_12,
          color: { QB: '#a78bfa', RB: '#4ade80', WR: '#60a5fa', TE: '#fbbf24', K: '#f472b6', DST: '#94a3b8' }[s.position] })),
          { fmt: (v) => fmt.n(v, 0) }),
        el('div', { class: 'note', style: 'margin-top:8px' },
          'Points you lose by waiting 12 more picks at each position.')),
      el('div', { class: 'card' },
        el('h3', {}, `My roster (${myRoster.length})`),
        myRoster.length ? myRoster.map((p) => el('div', { class: 'roster-slot' },
          el('span', {}, posTag(p.position || '—'), ' ', p.player_name || p.player_id),
          el('span', { class: 'num' }, fmt.n(p.proj_points, 0))))
          : el('div', { class: 'note' }, 'No picks yet.')),
      el('div', { class: 'card' },
        el('h3', {}, 'Recent picks'),
        (st.picks || []).slice(-12).reverse().map((p) => el('div', { class: 'kv' },
          el('span', { class: 'dim' }, `#${p.pick} · T${p.team_slot}`),
          el('span', {}, p.player?.player_name || p.player?.player_id || '—')))
        || el('div', { class: 'note' }, 'Draft has not started.')));
  }

  async function recommend(evt, container) {
    const btn = evt.target;
    btn.disabled = true; btn.textContent = 'simulating…';
    try {
      const r = await api.post('/api/draft/recommend', { n_sims: 200, top_k: 12 });
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
    const data = await api.get(`/api/games?week=${store.week}&n_sims=10000`);
    const cards = data.games.map((g) => el('div', { class: 'card game-card',
      onclick: () => gameDrawer(g.game_id, store.week) },
      el('div', { style: 'display:flex;justify-content:space-between;align-items:baseline' },
        el('div', {}, el('b', {}, g.away_team), el('span', { class: 'dim' }, ' at '), el('b', {}, g.home_team)),
        el('span', { class: 'dim', style: 'font-size:11px' }, `W${g.week}`)),
      el('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin:10px 0' },
        el('span', { class: 'score' }, `${fmt.n(g.proj_away_score, 1)} – ${fmt.n(g.proj_home_score, 1)}`),
        el('span', { class: 'pill' }, `${fmt.pct(Math.max(g.home_win_prob, g.away_win_prob), 0)} `
          + `${g.home_win_prob > g.away_win_prob ? g.home_team : g.away_team}`)),
      el('div', { class: 'kv' }, el('span', { class: 'dim' }, 'Market'),
        el('b', {}, `${g.market_spread > 0 ? g.home_team : g.away_team} -${fmt.n(Math.abs(g.market_spread), 1)} · O/U ${fmt.n(g.market_total, 1)}`)),
      el('div', { class: 'kv' }, el('span', { class: 'dim' }, 'Model edge'),
        el('b', { class: Math.abs(g.spread_edge) > 1.5 ? 'good' : 'dim' },
          `${fmt.signed(g.spread_edge, 1)} spread · ${fmt.signed(g.total_edge, 1)} total`)),
      el('div', { class: 'kv' }, el('span', { class: 'dim' }, 'Over'),
        el('b', {}, fmt.pct(g.over_prob, 0)))));
    body.replaceChildren(el('div', { class: 'grid g3' }, cards));
  }
  await load();
}

async function gameDrawer(gameId, week) {
  openDrawer(el('div', { class: 'empty' }, el('span', { class: 'loading' }), ' building breakdown'));
  try {
    const d = await api.get(`/api/games/${gameId}?week=${week}&n_sims=10000`);
    const s = d.summary;
    const teamCard = (b, label) => el('div', { class: 'card' },
      el('h3', {}, `${label} · ${b.team}`),
      kv('Fantasy points', fmt.n(b.totals.fantasy_points, 1)),
      kv('Passing yards', fmt.n(b.totals.passing_yards, 0)),
      kv('Rushing yards', fmt.n(b.totals.rushing_yards, 0)),
      kv('Touchdowns', fmt.n(b.totals.total_tds, 2)),
      kv('Targets / carries', `${fmt.n(b.totals.targets, 1)} / ${fmt.n(b.totals.carries, 1)}`),
      el('div', { style: 'margin-top:10px' },
        barChart(b.players.slice(0, 8).map((p) => ({ label: p.player_name, value: p.proj_points,
          color: { QB: '#a78bfa', RB: '#4ade80', WR: '#60a5fa', TE: '#fbbf24', K: '#f472b6', DST: '#94a3b8' }[p.position] })),
          { fmt: (v) => fmt.n(v, 1) })));

    openDrawer(el('div', {},
      el('h2', { style: 'margin:0 0 4px' }, `${s.away_team} at ${s.home_team}`),
      el('div', { class: 'dim', style: 'margin-bottom:16px' },
        `Week ${s.week} · market ${fmt.signed(s.market_spread, 1)} / ${fmt.n(s.market_total, 1)}`),
      el('div', { class: 'grid g4', style: 'margin-bottom:14px' },
        el('div', { class: 'card' }, stat('Predicted', `${fmt.n(s.proj_away_score, 1)}–${fmt.n(s.proj_home_score, 1)}`, 'away – home')),
        el('div', { class: 'card' }, stat('Total', fmt.n(s.proj_total, 1), `market ${fmt.n(s.market_total, 1)} · over ${fmt.pct(s.over_prob, 0)}`)),
        el('div', { class: 'card' }, stat('Margin', fmt.signed(s.proj_margin, 1), `±${fmt.n(s.margin_sd, 1)} sd`)),
        el('div', { class: 'card' }, stat('Home win', fmt.pct(s.home_win_prob, 1), `cover ${fmt.pct(s.home_cover_prob, 0)}`))),
      el('div', { class: 'grid g2', style: 'margin-bottom:14px' },
        el('div', { class: 'card' }, el('h3', {}, 'Simulated total'),
          histogram(d.distribution.total, { marker: s.market_total })),
        el('div', { class: 'card' }, el('h3', {}, 'Simulated margin'),
          histogram(d.distribution.margin, { marker: s.market_spread, color: '#63b3ed' }))),
      el('div', { class: 'grid g2', style: 'margin-bottom:14px' },
        teamCard(d.away_breakdown, 'Away'), teamCard(d.home_breakdown, 'Home')),
      el('div', { class: 'card', style: 'margin-bottom:14px' },
        el('h3', {}, 'Alternate lines'),
        el('div', { class: 'grid g2' },
          table(d.alt_lines.spreads, [
            { key: 'line', label: 'Spread', cls: 'num', fmt: (v) => fmt.signed(v, 1) },
            { key: 'home_cover', label: `${s.home_team} covers`, cls: 'num', fmt: (v) => fmt.pct(v, 1) }], {}),
          table(d.alt_lines.totals, [
            { key: 'line', label: 'Total', cls: 'num', fmt: (v) => fmt.n(v, 1) },
            { key: 'over', label: 'Over', cls: 'num', fmt: (v) => fmt.pct(v, 1) }], {}))),
      el('div', { class: 'card' },
        el('h3', {}, 'Closed-form cross-check (Stern model)'),
        kv('Home win probability', fmt.pct(d.analytic.home_win_prob, 1)),
        kv('Implied volatility', fmt.n(d.analytic.implied_volatility, 2)),
        el('div', { class: 'note', style: 'margin-top:8px' },
          'The Brownian-motion model prices the same game analytically from the spread and total alone. '
          + 'A large gap against the simulation usually means a lineup or usage assumption is doing the work.'))));
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
      m.venues.map((v) => el('div', { class: 'card' },
        el('div', { class: 'card-head' }, el('h3', {}, v.venue),
          el('span', { class: `pill ${v.reachable ? 'ok' : 'off'}` }, v.reachable ? 'reachable' : 'unreachable')),
        kv('Authenticated', v.authenticated ? 'yes' : 'read-only'),
        v.reachable ? null : el('div', { class: 'note', style: 'margin-top:8px' }, v.detail))),
      el('div', { class: 'card' },
        el('h3', {}, 'Paper account'),
        kv('Cash', fmt.money(portfolio.paper.cash)),
        kv('Equity', fmt.money(portfolio.paper.equity)),
        kv('Open positions', portfolio.paper.positions.length),
        kv('Mode', portfolio.mode)));

    const scanBtn = el('button', { class: 'btn primary', onclick: async (e) => {
      e.target.disabled = true; e.target.textContent = 'scanning…';
      try {
        const r = await api.post('/api/markets/signals', { week: store.week, min_edge: 0.02, n_sims: 10000 });
        signalBox.replaceChildren(signalTable(r));
        toast(`${r.signals.length} edges across ${r.markets_scanned} markets`);
      } catch (err) { toast(String(err), true); }
      e.target.disabled = false; e.target.textContent = 'Scan for edges';
    } }, 'Scan for edges');

    const signalBox = el('div', { style: 'margin-top:14px' },
      el('div', { class: 'note' }, 'Run a scan to price every listed NFL contract against the simulation.'));

    const tradesCard = el('div', { class: 'card', style: 'margin-top:14px' },
      el('h3', {}, 'Trade log'),
      table(portfolio.trades, [
        { key: 'ts', label: 'Time', fmt: (v) => fmt.time(v) },
        { key: 'mode', label: 'Mode', fmt: (v) => el('span', { class: v === 'live' ? 'warn' : 'dim' }, v) },
        { key: 'venue', label: 'Venue', cls: 'dim' },
        { key: 'market_id', label: 'Market' },
        { key: 'side', label: 'Side' },
        { key: 'quantity', label: 'Qty', cls: 'num', fmt: (v) => fmt.i(v) },
        { key: 'price', label: 'Price', cls: 'num', fmt: (v) => fmt.n(v, 3) },
        { key: 'status', label: 'Status', cls: 'dim' },
      ], { empty: 'No trades yet' }));

    wrap.replaceChildren(venueCards,
      el('div', { class: 'card', style: 'margin-top:14px' },
        el('div', { class: 'card-head' }, el('h3', {}, 'Model vs market'), scanBtn), signalBox),
      tradesCard);
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
    return el('div', { class: 'card' },
      el('div', { style: 'font-size:12.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' },
        m.title || m.market_id),
      el('div', { class: 'dim', style: 'font-size:11px;margin-bottom:8px' }, m.venue),
      el('div', { style: 'display:flex;justify-content:space-between;align-items:baseline' },
        el('span', { class: 'score' }, m.mid != null ? fmt.n(m.mid * 100, 1) + '¢' : '—'),
        el('span', { class: drift >= 0 ? 'good' : 'bad', style: 'font-size:12px' },
          `${drift >= 0 ? '▲' : '▼'} ${fmt.n(Math.abs(drift) * 100, 2)}¢`)),
      sparkline(hist, { height: 46 }),
      el('div', { style: 'margin-top:8px' },
        kv('Micro-price', m.micro_price != null ? `${fmt.n(m.micro_price * 100, 2)}¢` : '—'),
        kv('Spread', m.spread != null ? `${fmt.n(m.spread * 100, 1)}¢` : '—'),
        kv('Book imbalance', fmt.n(m.imbalance, 3)),
        kv('Momentum (z)', fmt.n(m.momentum, 2)),
        kv('Realised vol', fmt.n((m.realised_vol || 0) * 100, 3))));
  }

  try {
    const s = await api.get('/api/status');
    if (s.polling) connect();
  } catch { /* status is optional here */ }
}
