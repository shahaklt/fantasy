// Dashboard, Projections, Settings and My League views.
import { $, api, bar, barChart, el, fmt, histogram, kv, posTag, sparkline, stat, table, toast } from './ui.js';

export const store = { status: null, league: null, board: [], week: 1 };

// ------------------------------------------------------------------ helpers
export function openDrawer(node) {
  $('#drawer-body').replaceChildren(node);
  $('#drawer').classList.add('open');
}
export function closeDrawer() { $('#drawer').classList.remove('open'); }

function positionFilter(onChange, value = 'ALL') {
  const seg = el('div', { class: 'seg' });
  ['ALL', 'QB', 'RB', 'WR', 'TE', 'K', 'DST'].forEach((p) => {
    seg.append(el('button', {
      class: p === value ? 'active' : '',
      onclick: (e) => {
        [...seg.children].forEach((c) => c.classList.remove('active'));
        e.target.classList.add('active');
        onChange(p);
      },
    }, p));
  });
  return seg;
}

// ------------------------------------------------------------------ DASHBOARD
export async function dashboard(root) {
  root.replaceChildren(el('div', { class: 'empty' }, el('span', { class: 'loading' }), ' loading'));
  const s = await api.get('/api/status');
  store.status = s;
  store.league = s.league;

  const meta = s.meta || {};
  const proj = meta.projection_summary || {};
  const cards = el('div', { class: 'grid g4' },
    el('div', { class: 'card' }, stat('Season', s.season, `${proj.players || 0} players projected`)),
    el('div', { class: 'card' }, stat('Compute', s.compute.gpu ? 'GPU' : 'CPU', s.compute.backend)),
    el('div', { class: 'card' }, stat('Simulations', fmt.i(meta.n_sims || 0), meta.built_at ? `built ${meta.built_at}` : 'not built yet')),
    el('div', { class: 'card' }, stat('Trading', s.trading_mode.toUpperCase(),
      s.risk?.kill_switch ? 'KILL SWITCH ON' : `daily $${fmt.n(s.risk?.daily_notional || 0, 0)} used`)));

  const sched = s.scheduler || {};
  const schedCard = el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('h3', {}, 'Auto-scrape schedule'),
      el('div', { style: 'display:flex;gap:6px' },
        el('span', { class: `pill ${sched.running ? 'ok' : 'off'}` }, sched.running ? 'running' : 'stopped'),
        el('button', {
          class: 'btn sm',
          onclick: async () => {
            await api.post(sched.running ? '/api/scheduler/stop' : '/api/scheduler/start');
            dashboard(root);
          },
        }, sched.running ? 'Stop' : 'Start'))),
    kv('Runs daily at', (sched.times || []).join('  ·  ')),
    kv('Next run', sched.next_run || '—'),
    kv('Jobs', (sched.jobs || []).join(', ')),
    el('div', { style: 'display:flex;gap:6px;margin-top:10px;flex-wrap:wrap' },
      (sched.jobs || []).map((j) => el('button', {
        class: 'btn sm ghost',
        onclick: async (e) => {
          e.target.disabled = true; e.target.textContent = 'running…';
          try { const r = await api.post(`/api/scheduler/run/${j}`); toast(`${j}: ${r.ok ? 'done' : 'failed'}`, !r.ok); }
          catch (err) { toast(String(err), true); }
          dashboard(root);
        },
      }, `Run ${j}`))),
    el('div', { style: 'margin-top:12px' },
      el('h3', {}, 'Recent runs'),
      (sched.history || []).length
        ? table(sched.history, [
            { key: 'job', label: 'Job' },
            { key: 'started', label: 'Started' },
            { key: 'ok', label: 'Result', fmt: (v) => el('span', { class: v ? 'good' : 'bad' }, v ? 'ok' : 'failed') },
            { key: 'detail', label: 'Detail', fmt: (v) => el('span', { class: 'dim' }, JSON.stringify(v).slice(0, 90)) },
          ], { empty: 'No runs yet' })
        : el('div', { class: 'note' }, 'No runs recorded yet.')));

  const leagueCard = el('div', { class: 'card' },
    el('h3', {}, 'League'),
    kv('Name', s.league.name),
    kv('Teams', s.league.teams),
    kv('Scoring', s.league.scoring_preset),
    kv('Roster', Object.entries(s.league.roster).map(([k, v]) => `${v}${k}`).join(' · ')),
    kv('Your slot', s.league.draft_slot),
    kv('Draft type', s.league.draft_type));

  const venueCard = el('div', { class: 'card' },
    el('h3', {}, 'Venues'),
    ...(s.venues || []).map((v) => el('div', { class: 'kv' },
      el('span', {}, v.venue),
      el('span', { class: `pill ${v.authenticated ? 'ok' : ''}` }, v.authenticated ? 'authenticated' : 'read-only'))),
    el('div', { class: 'note', style: 'margin-top:10px' },
      'Read-only market data needs no credentials. Order placement stays in paper mode until you set '
      + 'GRIDIRON_TRADING_MODE=live and confirm each order.'));

  root.replaceChildren(cards,
    el('div', { class: 'grid g2', style: 'margin-top:14px' }, schedCard,
      el('div', { class: 'grid', style: 'gap:14px;align-content:start' }, leagueCard, venueCard)));
}

// ---------------------------------------------------------------- PROJECTIONS
export async function projections(root) {
  const controls = el('div', { class: 'controls' });
  const body = el('div', {});
  root.replaceChildren(controls, body);

  const state = { position: 'ALL', search: '', limit: 300 };

  const load = async () => {
    body.replaceChildren(el('div', { class: 'empty' }, el('span', { class: 'loading' }), ' simulating'));
    const q = new URLSearchParams({ position: state.position, limit: state.limit });
    if (state.search) q.set('search', state.search);
    const data = await api.get(`/api/players?${q}`);
    store.board = data.players;
    body.replaceChildren(el('div', { class: 'card pad0' }, table(data.players, PLAYER_COLS, {
      sortKey: 'proj_points', onRow: (r) => playerDrawer(r.player_id),
      empty: 'No players match those filters',
    })));
  };

  controls.append(
    positionFilter((p) => { state.position = p; load(); }),
    el('input', { type: 'search', placeholder: 'Search player…', oninput: (e) => {
      state.search = e.target.value; clearTimeout(load._t); load._t = setTimeout(load, 220);
    } }),
    el('span', { class: 'note' }, 'Click a row for the full distribution'));
  await load();
}

const PLAYER_COLS = [
  { key: 'player_name', label: 'Player', fmt: (v, r) => el('span', {}, posTag(r.position), ' ', v) },
  { key: 'team', label: 'Tm', cls: 'dim' },
  { key: 'proj_points', label: 'Proj', cls: 'num', fmt: (v) => fmt.n(v, 1) },
  { key: 'proj_ppg', label: 'PPG', cls: 'num', fmt: (v) => fmt.n(v, 1) },
  { key: 'proj_games', label: 'G', cls: 'num', fmt: (v) => fmt.n(v, 1) },
  { key: 'p5', label: 'Floor', cls: 'num dim', fmt: (v) => fmt.n(v, 0) },
  { key: 'p95', label: 'Ceiling', cls: 'num dim', fmt: (v) => fmt.n(v, 0) },
  { key: 'vorp', label: 'VORP', cls: 'num', fmt: (v) => el('span', { class: v > 0 ? 'good' : 'dim' }, fmt.n(v, 0)) },
  { key: 'auction_value', label: '$', cls: 'num', fmt: (v) => fmt.money(v) },
  { key: 'tier', label: 'Tier', cls: 'num', fmt: (v) => el('span', { class: 'tier' }, v ?? '—') },
  { key: 'adp', label: 'ADP', cls: 'num dim', fmt: (v) => fmt.n(v, 1) },
];

export async function playerDrawer(playerId) {
  openDrawer(el('div', { class: 'empty' }, el('span', { class: 'loading' }), ' loading player'));
  try {
    const p = await api.get(`/api/players/${playerId}`);
    const inputs = p.projection_inputs || {};
    const weekly = p.weekly || [];
    const node = el('div', {},
      el('h2', { style: 'margin:0 0 4px' }, p.player_name),
      el('div', { class: 'dim', style: 'margin-bottom:16px' },
        `${p.position} · ${p.team} · tier ${p.tier ?? '—'} · ADP ${fmt.n(p.adp, 1)}`),
      el('div', { class: 'grid g4', style: 'margin-bottom:16px' },
        el('div', { class: 'card' }, stat('Projected', fmt.n(p.proj_points, 0), `${fmt.n(p.proj_ppg, 1)} per game`)),
        el('div', { class: 'card' }, stat('Games', fmt.n(p.proj_games, 1), 'expected')),
        el('div', { class: 'card' }, stat('VORP', fmt.n(p.vorp, 0), `auction ${fmt.money(p.auction_value)}`)),
        el('div', { class: 'card' }, stat('Range', `${fmt.n(p.p5, 0)}–${fmt.n(p.p95, 0)}`, '90% interval'))),
      el('div', { class: 'card', style: 'margin-bottom:14px' },
        el('h3', {}, 'Season outcome distribution'),
        histogram(p.distribution, { marker: p.proj_points })),
      weekly.length ? el('div', { class: 'card', style: 'margin-bottom:14px' },
        el('h3', {}, 'Week by week'),
        sparkline(weekly.map((w) => w.mean), { height: 70, color: '#4fd1c5' }),
        el('div', { class: 'note', style: 'margin-top:6px' },
          `Weeks ${weekly[0].week}–${weekly[weekly.length - 1].week}; the dip is the bye.`)) : null,
      el('div', { class: 'grid g2' },
        el('div', { class: 'card' },
          el('h3', {}, 'Projected usage'),
          kv('Target share', fmt.pct(inputs.target_share_proj)),
          kv('Carry share', fmt.pct(inputs.carry_share_proj)),
          kv('Dropback share', fmt.pct(inputs.dropback_share_proj)),
          kv('Targets / game', fmt.n(inputs.targets_pg_proj, 1)),
          kv('Carries / game', fmt.n(inputs.carries_pg_proj, 1))),
        el('div', { class: 'card' },
          el('h3', {}, 'Efficiency & context'),
          kv('Yards per target', fmt.n(inputs.yards_per_target, 2)),
          kv('Catch rate', fmt.pct(inputs.catch_rate)),
          kv('Yards per carry', fmt.n(inputs.rush_ypc, 2)),
          kv('Role confidence', fmt.pct(inputs.role_confidence)),
          kv('Age', fmt.n(p.age, 1)),
          kv('Bye week', p.bye ?? '—'))));
    openDrawer(node);
  } catch (err) {
    openDrawer(el('div', { class: 'empty bad' }, String(err)));
  }
}

// ------------------------------------------------------------------ SETTINGS
export async function settings(root) {
  const s = await api.get('/api/status');
  const league = s.league;
  const form = el('div', { class: 'grid g2' });

  const field = (label, input) => el('label', { class: 'field', style: 'margin-bottom:10px' }, label, input);
  const num = (name, value, min, max) => el('input', { type: 'number', value, min, max, id: `f-${name}` });

  const rosterKeys = ['QB', 'RB', 'WR', 'TE', 'FLEX', 'SUPERFLEX', 'K', 'DST', 'BN'];
  const rosterInputs = el('div', { class: 'grid g3' },
    rosterKeys.map((k) => field(k, el('input', { type: 'number', min: 0, max: 10,
      value: league.roster[k] ?? 0, id: `r-${k}` }))));

  const scoringSel = el('select', { id: 'f-scoring' },
    ['half_ppr', 'ppr', 'standard', 'te_premium', 'draftkings'].map((p) =>
      el('option', { value: p, selected: p === league.scoring_preset ? 'selected' : null }, p)));
  const draftSel = el('select', { id: 'f-draft' },
    ['snake', 'linear', 'auction'].map((p) =>
      el('option', { value: p, selected: p === league.draft_type ? 'selected' : null }, p)));

  form.append(
    el('div', { class: 'card' },
      el('h3', {}, 'League'),
      field('Name', el('input', { value: league.name, id: 'f-name' })),
      field('Teams', num('teams', league.teams, 2, 32)),
      field('Scoring preset', scoringSel),
      field('Draft type', draftSel),
      field('Your draft slot', num('slot', league.draft_slot, 1, 32)),
      field('Auction budget', num('budget', league.auction_budget, 1, 1000)),
      field('Regular season weeks', num('weeks', league.regular_season_weeks, 1, 18)),
      field('Playoff teams', num('playoff', league.playoff_teams, 2, 16)),
      el('button', {
        class: 'btn primary', style: 'margin-top:10px',
        onclick: async (e) => {
          e.target.disabled = true;
          const roster = {};
          rosterKeys.forEach((k) => {
            const v = Number($(`#r-${k}`).value);
            if (v > 0) roster[k] = v;
          });
          try {
            await api.post('/api/league', {
              name: $('#f-name').value,
              teams: Number($('#f-teams').value),
              scoring_preset: $('#f-scoring').value,
              draft_type: $('#f-draft').value,
              draft_slot: Number($('#f-slot').value),
              auction_budget: Number($('#f-budget').value),
              regular_season_weeks: Number($('#f-weeks').value),
              playoff_teams: Number($('#f-playoff').value),
              roster,
            });
            toast('Saved — rebuilding simulations…');
            await api.post('/api/rebuild', { n_sims: 5000 });
            toast('Rebuilt with the new settings');
          } catch (err) { toast(String(err), true); }
          e.target.disabled = false;
          settings(root);
        },
      }, 'Save & rebuild')),
    el('div', { class: 'grid', style: 'gap:14px;align-content:start' },
      el('div', { class: 'card' }, el('h3', {}, 'Roster slots'), rosterInputs),
      el('div', { class: 'card' },
        el('h3', {}, 'Risk limits'),
        ...Object.entries(s.risk?.limits || {}).map(([k, v]) => kv(k.replace(/_/g, ' '), String(v))),
        el('div', { class: 'note', style: 'margin-top:10px' },
          `Trading mode is ${s.trading_mode}. To place real orders, start the server with `
          + 'GRIDIRON_TRADING_MODE=live; every order still needs an explicit confirmation. '
          + `Create the file ${s.risk?.kill_switch_path} to halt all trading immediately.`)),
      el('div', { class: 'card' },
        el('h3', {}, 'Kalshi credentials'),
        field('Key ID', el('input', { id: 'k-key', placeholder: 'xxxxxxxx-xxxx-…' })),
        field('Private key path', el('input', { id: 'k-path', placeholder: '/home/you/.kalshi/key.pem' })),
        el('button', {
          class: 'btn', onclick: async () => {
            try {
              const r = await api.post('/api/credentials', {
                venue: 'kalshi', key_id: $('#k-key').value, private_key_path: $('#k-path').value });
              toast(r.detail || 'saved');
            } catch (err) { toast(String(err), true); }
          },
        }, 'Save reference'),
        el('div', { class: 'note', style: 'margin-top:8px' },
          'Only the path is stored locally — the key never leaves your machine and is never sent anywhere but Kalshi. '
          + 'For Polymarket, export POLYMARKET_PRIVATE_KEY before starting the server.'))));

  root.replaceChildren(form);
}

// ----------------------------------------------------------------- MY LEAGUE
export async function myLeague(root) {
  const wrap = el('div', {});
  root.replaceChildren(wrap);
  const board = store.board.length ? store.board : (await api.get('/api/players?limit=600')).players;
  store.board = board;

  const rosterIds = JSON.parse(localStorage.getItem('gridiron.myroster') || '[]');
  const save = () => localStorage.setItem('gridiron.myroster', JSON.stringify(rosterIds));

  const render = async () => {
    const mine = rosterIds.map((id) => board.find((b) => b.player_id === id)).filter(Boolean);
    const picker = el('select', { id: 'add-player' },
      el('option', { value: '' }, 'Add a player…'),
      board.slice(0, 500).map((p) => el('option', { value: p.player_id },
        `${p.player_name} (${p.position} ${p.team})`)));

    const left = el('div', { class: 'card' },
      el('div', { class: 'card-head' }, el('h3', {}, `My roster (${mine.length})`),
        el('button', { class: 'btn sm ghost', onclick: () => { rosterIds.length = 0; save(); render(); } }, 'Clear')),
      el('div', { style: 'display:flex;gap:8px;margin-bottom:10px' }, picker,
        el('button', {
          class: 'btn sm', onclick: () => {
            const v = $('#add-player').value;
            if (v && !rosterIds.includes(v)) { rosterIds.push(v); save(); render(); }
          },
        }, 'Add')),
      mine.length ? mine.map((p) => el('div', { class: 'roster-slot' },
        el('span', {}, posTag(p.position), ' ', p.player_name, ' ', el('span', { class: 'dim' }, p.team)),
        el('span', {}, el('span', { class: 'num' }, fmt.n(p.proj_points, 0)), ' ',
          el('button', { class: 'btn sm ghost', onclick: () => {
            rosterIds.splice(rosterIds.indexOf(p.player_id), 1); save(); render();
          } }, '✕'))))
        : el('div', { class: 'note' }, 'Add players to see start/sit and playoff odds.'));

    const right = el('div', { class: 'grid', style: 'gap:14px;align-content:start' });
    if (mine.length >= 1) {
      const weekSel = el('select', { id: 'ls-week' },
        Array.from({ length: 18 }, (_, i) => el('option', { value: i + 1, selected: i + 1 === store.week ? 'selected' : null }, `Week ${i + 1}`)));
      right.append(el('div', { class: 'card' },
        el('div', { class: 'card-head' }, el('h3', {}, 'Start / sit'), weekSel,
          el('button', {
            class: 'btn sm', onclick: async (e) => {
              e.target.disabled = true;
              try {
                const rows = await api.post('/api/lineup', {
                  player_ids: rosterIds, week: Number($('#ls-week').value) });
                e.target.parentElement.parentElement.append(el('div', { class: 'card pad0', style: 'margin-top:10px' },
                  table(rows, [
                    { key: 'player_name', label: 'Player', fmt: (v, r) => el('span', {}, posTag(r.position), ' ', v) },
                    { key: 'proj_points', label: 'Proj', cls: 'num', fmt: (v) => fmt.n(v, 1) },
                    { key: 'floor', label: 'Floor', cls: 'num dim', fmt: (v) => fmt.n(v, 1) },
                    { key: 'ceiling', label: 'Ceiling', cls: 'num dim', fmt: (v) => fmt.n(v, 1) },
                    { key: 'start_pct', label: 'Start %', cls: 'num',
                      fmt: (v) => el('span', { class: v > 0.6 ? 'good' : v > 0.25 ? 'warn' : 'dim' }, fmt.pct(v, 0)) },
                  ], { sortKey: 'start_pct' })));
              } catch (err) { toast(String(err), true); }
              e.target.disabled = false;
            },
          }, 'Optimise'))));

      const total = mine.reduce((a, p) => a + (p.proj_points || 0), 0);
      right.append(el('div', { class: 'card' },
        el('h3', {}, 'Roster strength'),
        kv('Total projected points', fmt.n(total, 0)),
        kv('Best player', mine.slice().sort((a, b) => b.proj_points - a.proj_points)[0]?.player_name || '—'),
        el('div', { style: 'margin-top:10px' },
          barChart(mine.slice().sort((a, b) => b.proj_points - a.proj_points).slice(0, 10)
            .map((p) => ({ label: p.player_name, value: p.proj_points,
              color: { QB: '#a78bfa', RB: '#4ade80', WR: '#60a5fa', TE: '#fbbf24', K: '#f472b6', DST: '#94a3b8' }[p.position] })),
            { fmt: (v) => fmt.n(v, 0) }))));
    }
    wrap.replaceChildren(el('div', { class: 'grid g2' }, left, right));
  };
  await render();
}
