// Dashboard, Projections, Settings and My League views.
import { $, api, bar, barChart, bounds, distBar, el, fmt, gauge, heatCell, histogram, kv,
        led, metric, panel, posTag, SIMS, sparkline, table, toast } from './ui.js';

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
  root.replaceChildren(el('div', { class: 'empty' }, 'loading…'));
  const s = await api.get('/api/status');
  store.status = s;
  store.league = s.league;

  const meta = s.meta || {};
  const proj = meta.projection_summary || {};
  const sched = s.scheduler || {};
  const risk = s.risk || {};

  const top = el('div', { class: 'grid g4' },
    panel('Season', gauge('active', String(s.season), `${proj.players || 0} players projected`)),
    panel('Compute', gauge('backend', s.compute.gpu ? 'GPU' : 'CPU', s.compute.backend)),
    panel('Simulations', gauge('per player', fmt.i(meta.n_sims || 0),
      meta.built_at ? `built ${meta.built_at}` : 'not built yet')),
    panel('Trading', gauge('mode', s.trading_mode.toUpperCase(),
      risk.kill_switch ? 'KILL SWITCH ENGAGED' : `$${fmt.n(risk.daily_notional || 0, 0)} of $${fmt.n(risk.limits?.max_daily_notional || 0, 0)} today`,
      risk.limits?.max_daily_notional ? (risk.daily_notional || 0) / risk.limits.max_daily_notional : null)));

  const schedPanel = panel('Auto-scrape',
    el('div', {},
      metric('runs daily at', (sched.times || []).join('  ·  '), true),
      metric('next run', sched.next_run ? sched.next_run.replace('T', '  ') : '—'),
      metric('jobs', String((sched.jobs || []).length)),
      el('div', { style: 'display:flex;gap:6px;margin-top:12px;flex-wrap:wrap' },
        (sched.jobs || []).map((j) => el('button', {
          class: 'btn sm ghost',
          onclick: async (e) => {
            e.target.disabled = true; e.target.textContent = 'running';
            try { const r = await api.post(`/api/scheduler/run/${j}`); toast(`${j}: ${r.ok ? 'ok' : 'failed'}`, !r.ok); }
            catch (err) { toast(String(err), true); }
            dashboard(root);
          },
        }, j.replace(/_/g, ' ')))),
      (sched.history || []).length
        ? el('div', { style: 'margin-top:14px' }, table((sched.history || []).slice().reverse(), [
            { key: 'job', label: 'Job', fmt: (v) => v.replace(/_/g, ' ') },
            { key: 'started', label: 'Started', cls: 'dim', fmt: (v) => (v || '').replace('T', ' ') },
            { key: 'ok', label: 'Result', fmt: (v) => el('span', { class: v ? 'good' : 'bad' }, v ? 'ok' : 'failed') },
          ], { empty: 'No runs recorded' }))
        : el('div', { class: 'note', style: 'margin-top:12px' }, 'No runs recorded yet.')),
    { meta: sched.running ? 'running' : 'stopped',
      actions: [led(sched.running ? 'on' : 'off'), el('button', {
        class: 'btn sm',
        onclick: async () => { await api.post(sched.running ? '/api/scheduler/stop' : '/api/scheduler/start'); dashboard(root); },
      }, sched.running ? 'Stop' : 'Start')] });

  const leaguePanel = panel('League',
    el('div', {},
      metric('name', s.league.name),
      metric('teams', String(s.league.teams)),
      metric('scoring', s.league.scoring_preset),
      metric('draft', `${s.league.draft_type} · slot ${s.league.draft_slot}`),
      metric('roster', Object.entries(s.league.roster).map(([k, v]) => `${v}${k}`).join(' '))));

  const venuePanel = panel('Venues',
    el('div', {},
      (s.venues || []).map((v) => el('div', { class: 'metric' },
        el('span', { class: 'k' }, v.venue),
        el('span', { class: 'v' },
          el('span', { class: `pill ${v.authenticated ? 'ok' : ''}` },
            v.authenticated ? 'authed' : 'read-only')))),
      el('div', { class: 'note', style: 'margin-top:10px' },
        'Market data needs no credentials. Orders stay in paper mode until you '
        + 'start the server with GRIDIRON_TRADING_MODE=live and confirm each one.')));

  root.replaceChildren(top,
    el('div', { class: 'grid g2', style: 'margin-top:14px' }, schedPanel,
      el('div', { class: 'grid', style: 'gap:14px;align-content:start' }, leaguePanel, venuePanel)));
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
    const rows = data.players;
    const b = { pts: bounds(rows, 'proj_points'), vorp: bounds(rows, 'vorp'),
                ppg: bounds(rows, 'proj_ppg'), g: bounds(rows, 'proj_games') };
    const lo = Math.min(...rows.map((r) => r.p5 ?? 0));
    const hi = Math.max(...rows.map((r) => r.p95 ?? 1));
    body.replaceChildren(panel('Projections',
      table(rows, playerCols(b, lo, hi), {
        sortKey: 'proj_points', onRow: (r) => playerDrawer(r.player_id),
        empty: 'No players match those filters',
      }), { flush: true, meta: `${data.count} players` }));
  };

  controls.append(
    positionFilter((p) => { state.position = p; load(); }),
    el('input', { type: 'search', placeholder: 'Search player…', oninput: (e) => {
      state.search = e.target.value; clearTimeout(load._t); load._t = setTimeout(load, 220);
    } }),
    el('span', { class: 'note' }, 'Click a row for the full distribution'));
  await load();
}

/** Columns carry their own shading, so a scan down one tells you the shape. */
function playerCols(b, distLo, distHi) {
  return [
    { key: 'player_name', label: 'Player', cls: 'name',
      fmt: (v, r) => el('span', {}, posTag(r.position), ' ', v) },
    { key: 'team', label: 'Tm', cls: 'dim' },
    { key: 'proj_points', label: 'Proj', cls: 'num',
      cellAttrs: (v) => heatCell(v, b.pts[0], b.pts[1]), fmt: (v) => fmt.n(v, 0) },
    { key: 'proj_ppg', label: 'PPG', cls: 'num',
      cellAttrs: (v) => heatCell(v, b.ppg[0], b.ppg[1]), fmt: (v) => fmt.n(v, 1) },
    { key: 'proj_games', label: 'G', cls: 'num',
      cellAttrs: (v) => heatCell(v, b.g[0], b.g[1]), fmt: (v) => fmt.n(v, 1) },
    { key: 'p50', label: 'Range', sortable: false,
      fmt: (v, r) => distBar(r.p5, r.p50 ?? r.proj_points, r.p95, distLo, distHi) },
    { key: 'vorp', label: 'VORP', cls: 'num',
      cellAttrs: (v) => heatCell(v, b.vorp[0], b.vorp[1]),
      fmt: (v) => el('span', { class: v > 0 ? 'good' : 'dim' }, fmt.n(v, 0)) },
    { key: 'auction_value', label: '$', cls: 'num', fmt: (v) => fmt.money(v) },
    { key: 'tier', label: 'Tier', cls: 'num', fmt: (v) => el('span', { class: 'tier' }, v ?? '—') },
    { key: 'adp', label: 'ADP', cls: 'num dim', fmt: (v) => fmt.n(v, 1) },
  ];
}

export async function playerDrawer(playerId) {
  openDrawer(el('div', { class: 'empty' }, el('span', { class: 'loading' }), ' loading player'));
  try {
    const p = await api.get(`/api/players/${playerId}`);
    const inputs = p.projection_inputs || {};
    const weekly = p.weekly || [];
    const node = el('div', {},
      el('div', { style: 'margin-bottom:14px' },
        el('h2', { style: 'margin:0 0 3px;font:600 20px/1.2 var(--sans)' }, p.player_name),
        el('div', { class: 'note' },
          `${p.position} · ${p.team} · tier ${p.tier ?? '—'} · ADP ${fmt.n(p.adp, 1)} · bye ${p.bye ?? '—'}`)),
      el('div', { class: 'grid g4', style: 'margin-bottom:16px' },
        panel(null, gauge('Projected', fmt.n(p.proj_points, 0), `${fmt.n(p.proj_ppg, 1)} per game`)),
        panel(null, gauge('Games', fmt.n(p.proj_games, 1), 'expected')),
        panel(null, gauge('VORP', fmt.n(p.vorp, 0), `auction ${fmt.money(p.auction_value)}`)),
        panel(null, gauge('Range', `${fmt.n(p.p5, 0)}–${fmt.n(p.p95, 0)}`, '90% interval'))),
      el('div', { style: 'margin-bottom:14px' }, panel('Season outcome distribution',
        histogram(p.distribution, { marker: p.proj_points }),
        { meta: `${fmt.n(p.p5, 0)} – ${fmt.n(p.p95, 0)} at 90%` })),
      weekly.length
        ? el('div', { style: 'margin-bottom:14px' },
            panel('Week by week',
              el('div', {},
                sparkline(weekly.map((w) => w.mean), { height: 70, color: '#3ddbd9' }),
                el('div', { class: 'note', style: 'margin-top:6px' },
                  `Weeks ${weekly[0].week}–${weekly[weekly.length - 1].week}; the dip is the bye.`)),
              { meta: `${weekly.length} weeks` }))
        : null,
      el('div', { class: 'grid g2' },
        panel('Projected usage', el('div', {},
          kv('Target share', fmt.pct(inputs.target_share_proj)),
          kv('Carry share', fmt.pct(inputs.carry_share_proj)),
          kv('Dropback share', fmt.pct(inputs.dropback_share_proj)),
          kv('Targets / game', fmt.n(inputs.targets_pg_proj, 1)),
          kv('Carries / game', fmt.n(inputs.carries_pg_proj, 1)))),
        panel('Efficiency & context', el('div', {},
          kv('Yards per target', fmt.n(inputs.yards_per_target, 2)),
          kv('Catch rate', fmt.pct(inputs.catch_rate)),
          kv('Yards per carry', fmt.n(inputs.rush_ypc, 2)),
          kv('Role confidence', fmt.pct(inputs.role_confidence)),
          kv('Age', fmt.n(p.age, 1)),
          kv('Role', p.is_rookie ? 'rookie' : 'veteran')))));
    openDrawer(node);
  } catch (err) {
    openDrawer(el('div', { class: 'empty bad' }, String(err)));
  }
}

// ------------------------------------------------------------------ SETTINGS
/** What to check when the phone shows nothing at all.
 *
 * Worth spelling out because the three causes are indistinguishable from the
 * phone: in every one of them the page simply never arrives. A wrong token
 * looks different — it returns the "locked" page — so if you are reading a
 * spinner, it is one of these.
 */
function troubleshooting(acc) {
  const windows = (acc.platform || '').startsWith('win');
  const steps = [
    windows
      ? ['Windows Firewall', 'The first time Python listens on the network Windows asks '
        + 'whether to allow it, and a dismissed prompt blocks every phone silently. Allow '
        + 'Python on Private networks in Windows Defender Firewall.']
      : ['Firewall', 'Allow inbound connections to this port. On Linux: '
        + 'sudo ufw allow ' + (acc.listener_port || acc.port) + '/tcp'],
    ['Wrong address', 'A machine with a VM, Docker or a VPN has several addresses and only '
      + 'one reaches your phone. Try each tab above — that is what they are for.'],
    ['Different network', 'Phones drift onto guest wifi or cellular. Check the phone is on '
      + 'the same SSID, and that the router does not have client or AP isolation switched on.'],
  ];
  return el('details', { style: 'margin-top:12px' },
    el('summary', { style: 'cursor:pointer;font:11px/1.6 var(--mono);color:var(--ink-3)' },
      'Nothing loading on the phone?'),
    el('div', { style: 'margin-top:8px' },
      steps.map(([title, text]) => el('div', { class: 'note', style: 'margin-bottom:8px' },
        el('b', { style: 'color:var(--ink-2)' }, title + ' — '), text))));
}


/** Everything needed to open this panel on a phone, without typing a token.
 *
 * The panel re-renders itself in place rather than re-rendering the settings
 * view, so turning access on does not wipe whatever the fetch buttons above it
 * just wrote.
 */
async function phoneAccessPanel() {
  const host = el('div', {});

  async function render() {
    let acc;
    try {
      acc = await api.get('/api/access');
    } catch (err) {
      host.replaceChildren(panel('Phone access', el('div', { class: 'note bad' }, String(err))));
      return;
    }
    host.replaceChildren(acc.local ? localView(acc) : pairedView());
  }

  function pairedView() {
    // Reached from the phone itself. Handing the token back over the wire
    // would defeat the point of having one.
    return panel('Phone access', el('div', { class: 'note' },
      'This device is paired. The pairing link, token and QR code are only readable '
      + 'from the machine running the server.'), { actions: [led('on')] });
  }

  function toggleButton(acc) {
    if (!acc.can_toggle) return null;      // bound at startup; cannot be given back

    if (!acc.on_network) {
      return el('button', {
        class: 'btn primary',
        onclick: async (e) => {
          const ok = confirm(
            'Put this panel on your wifi?\n\n'
            + 'Every device on the network will be able to reach it, but only with the '
            + 'access token — which the QR code carries. This machine stays exempt.');
          if (!ok) return;
          e.target.disabled = true; e.target.textContent = 'opening';
          try {
            const r = await api.post('/api/access/enable');
            toast(r.already ? 'already on the network' : `phone access on, port ${r.port}`);
          } catch (err) { toast(String(err), true); }
          render();
        },
      }, 'Turn on phone access');
    }

    return el('button', {
      class: 'btn danger sm',
      onclick: async (e) => {
        e.target.disabled = true; e.target.textContent = 'closing';
        try {
          await api.post('/api/access/disable');
          toast('phone access off');
        } catch (err) { toast(String(err), true); }
        render();
      },
    }, 'Turn off');
  }

  function offView(acc) {
    const body = el('div', {},
      el('div', { class: 'note' },
        'The panel is bound to this machine only, so nothing else on the network can '
        + 'reach it. Turning phone access on opens a second listener on every interface '
        + 'and puts an access token in front of it. Nothing restarts, so the simulation '
        + 'cache and any draft in progress survive.'),
      el('div', { style: 'margin-top:10px' }, toggleButton(acc)),
      ...((acc.lan_addresses || []).length
        ? [kv('this machine is', acc.lan_addresses[0])]
        : []),
      el('div', { class: 'note', style: 'margin-top:10px' },
        'Prefer it on from the start? Launch with ./run.sh --lan (Windows: run.bat --lan).'));
    return panel('Phone access', body, { actions: [led('off')] });
  }

  function onView(acc) {
    // A machine with a VM, Docker or a VPN has several addresses and only one
    // of them reaches the sofa. Guessing wrong looks exactly like a firewall
    // block, so every candidate gets its own tab, link and code.
    const candidates = (acc.candidates || []).length
      ? acc.candidates
      : [{ address: null, url: acc.url, qr: acc.qr, routed: true, hint: '' }];
    let chosen = candidates.find((c) => c.routed) || candidates[0];

    const link = el('input', { value: chosen.url, readonly: 'readonly',
      onclick: (e) => e.target.select(), style: 'width:100%' });
    const qrBox = el('div', {});
    const hintLine = el('div', { class: 'note' });

    function showChosen() {
      qrBox.replaceChildren(chosen.qr
        ? el('div', { class: 'qr', html: chosen.qr })
        : el('div', { class: 'note bad' },
            'No QR code — install segno (pip install segno) and restart to get one.'));
      link.value = chosen.url;
      hintLine.className = chosen.hint ? 'note warn' : 'note';
      hintLine.textContent = chosen.hint
        || (candidates.length > 1
          ? 'If this one does not load, try the other addresses above.'
          : '');
    }

    showChosen();      // the box starts empty otherwise

    const chooser = candidates.length > 1
      ? el('div', { class: 'seg', style: 'margin-bottom:10px;flex-wrap:wrap' },
          candidates.map((c) => el('button', {
            class: c === chosen ? 'active' : '',
            title: c.hint || (c.routed ? 'the address your machine routes through' : ''),
            onclick: (e) => {
              chosen = c;
              [...e.target.parentElement.children].forEach((b) =>
                b.classList.toggle('active', b === e.target));
              showChosen();
            },
          }, c.address + (c.routed ? ' \u2713' : ''))))
      : null;

    const body = el('div', {},
      el('div', { class: 'note', style: 'margin-bottom:10px' },
        'Scan this with your phone camera while it is on the same wifi. The link carries '
        + 'the token, so the device is remembered after the first load.'),
      chooser,
      qrBox,
      hintLine,
      el('div', { style: 'margin-top:10px' }, link),
      el('div', { style: 'display:flex;gap:6px;margin-top:8px;flex-wrap:wrap' },
        el('button', {
          class: 'btn sm',
          onclick: async (e) => {
            try {
              await navigator.clipboard.writeText(acc.url);
              toast('link copied');
            } catch { link.select(); toast('select and copy the link', true); }
            e.target.blur();
          },
        }, 'Copy link'),
        el('button', {
          class: 'btn sm',
          onclick: async (e) => {
            if (!confirm('Issue a new token? Every paired device will have to scan again.')) return;
            e.target.disabled = true;
            try { await api.post('/api/access/rotate'); toast('new token issued'); }
            catch (err) { toast(String(err), true); }
            render();
          },
        }, acc.pinned_by_env ? 'Token pinned by env' : 'Rotate token'),
        toggleButton(acc)),
      kv('token', acc.token),
      ...(acc.listener_port ? [kv('listening on', `0.0.0.0:${acc.listener_port}`)] : []),
      troubleshooting(acc),
      el('div', { class: 'note', style: 'margin-top:10px' },
        acc.url.startsWith('https://')
          ? 'That is an HTTPS origin, so the phone can install the panel as a real app '
            + '(Add to Home Screen) and open it offline.'
          : 'Over plain wifi the browser will not install this as an offline app — that '
            + 'needs HTTPS. iOS Add to Home Screen still gives you a full-screen icon. '
            + 'For a properly installable app, or for access away from home, start the '
            + 'server with --tunnel, or put it behind Tailscale.'),
      ...(acc.can_toggle
        ? []
        : [el('div', { class: 'note' },
            'This server was started with --lan, so the network binding belongs to the '
            + 'process. Restart it without that flag to take the panel off the network.')]),
      ...(acc.tunnel_available
        ? [el('div', { class: 'note' }, 'cloudflared is installed: ./run.sh --tunnel exposes '
            + 'the panel off your network, still behind this token.')]
        : []));

    return panel('Phone access', body, { actions: [led('on')] });
  }

  function localView(acc) {
    return acc.on_network ? onView(acc) : offView(acc);
  }

  await render();
  return host;
}


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

  const dataPanel = panel('Data',
    el('div', {},
      el('div', { class: 'note', style: 'margin-bottom:12px' },
        'Source data refreshes on its own at 00:00 and 12:00. Fetch it by hand after '
        + 'an injury or a depth-chart change, then rebuild so the simulations use it.'),
      metric('last built', s.meta?.built_at || 'never'),
      metric('simulations', fmt.i(s.meta?.n_sims || 0)),
      metric('players', fmt.i(s.meta?.players || 0)),
      metric('sources', (s.meta?.market_sources || []).join(', ') || '—'),
      el('div', { style: 'display:flex;gap:6px;margin-top:12px;flex-wrap:wrap' },
        el('button', {
          class: 'btn primary',
          onclick: async (e) => {
            e.target.disabled = true; e.target.textContent = 'fetching';
            try {
              const r = await api.post('/api/refresh');
              const failed = Object.entries(r.steps).filter(([, v]) => v.startsWith('failed'));
              const box = $('#fetch-result');
              box.replaceChildren(...Object.entries(r.steps).map(([k, v]) =>
                el('div', { class: 'metric' },
                  el('span', { class: 'k' }, k.replace(/_/g, ' ')),
                  el('span', { class: `v ${v.startsWith('ok') ? '' : 'bad'}` }, v))));
              toast(failed.length
                ? `fetched with ${failed.length} source(s) unavailable`
                : `all sources fetched in ${r.seconds}s`, failed.length > 0);
            } catch (err) {
              toast(String(err), true);
              $('#fetch-result').replaceChildren(
                el('div', { class: 'note bad' }, String(err)));
            }
            // Deliberately not re-rendering: the per-source result is the
            // point of pressing the button, and a re-render would wipe it.
            e.target.disabled = false; e.target.textContent = 'Fetch new data';
          },
        }, 'Fetch new data'),
        el('button', {
          class: 'btn',
          onclick: async (e) => {
            e.target.disabled = true; e.target.textContent = 'queued';
            try {
              const r = await api.post('/api/rebuild', { n_sims: SIMS });
              toast(r.started ? 'rebuilding in the background' : r.detail);
            } catch (err) { toast(String(err), true); }
            e.target.disabled = false; e.target.textContent = 'Rebuild simulations';
          },
        }, 'Rebuild simulations'),
        el('button', {
          class: 'btn ghost',
          onclick: async (e) => {
            e.target.disabled = true; e.target.textContent = 'running';
            try {
              const r = await api.post('/api/rebuild', { n_sims: SIMS, refresh_data: true });
              toast(r.started ? 'fetching then rebuilding' : r.detail);
            } catch (err) { toast(String(err), true); }
            e.target.disabled = false; e.target.textContent = 'Fetch + rebuild';
          },
        }, 'Fetch + rebuild')),
      el('div', { id: 'fetch-result', style: 'margin-top:12px' })),
    { meta: s.build?.building ? 'building' : 'idle',
      actions: [led(s.build?.building ? 'busy' : 'on')] });

  form.append(dataPanel);
  form.append(await phoneAccessPanel());

  form.append(
    panel('League',
      el('div', {},
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
            await api.post('/api/rebuild', { n_sims: SIMS });
            toast('Rebuilt with the new settings');
          } catch (err) { toast(String(err), true); }
          e.target.disabled = false;
          settings(root);
        },
      }, 'Save & rebuild'),
      el('div', { class: 'note', style: 'margin-top:12px;padding-top:12px;border-top:1px solid var(--line)' },
        'Playing on ESPN? Connect your league instead of filling this in — it imports '
        + 'teams, scoring, roster slots, your players, your draft and your schedule.'),
      el('button', {
        class: 'btn ghost sm', style: 'margin-top:8px',
        onclick: () => { location.hash = 'espn'; },
      }, 'Connect ESPN league →'))),
    el('div', { class: 'grid', style: 'gap:14px;align-content:start' },
      panel('Roster slots', rosterInputs),
      panel('Risk limits',
        el('div', {},
        ...Object.entries(s.risk?.limits || {}).map(([k, v]) => kv(k.replace(/_/g, ' '), String(v))),
        el('div', { class: 'note', style: 'margin-top:10px' },
          `Trading mode is ${s.trading_mode}. To place real orders, start the server with `
          + 'GRIDIRON_TRADING_MODE=live; every order still needs an explicit confirmation. '
          + `Create the file ${s.risk?.kill_switch_path} to halt all trading immediately.`))),
      panel('Kalshi credentials',
        el('div', {},
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
          + 'For Polymarket, export POLYMARKET_PRIVATE_KEY before starting the server.')))));

  root.replaceChildren(form);
}

// ----------------------------------------------------------------- MY LEAGUE
export async function myLeague(root) {
  const wrap = el('div', {});
  root.replaceChildren(wrap);
  const board = store.board.length ? store.board : (await api.get('/api/players?limit=600')).players;
  store.board = board;

  // An ESPN-imported roster is authoritative; the manual list is the fallback
  // for people not on ESPN, so nobody has to enter their team twice.
  let imported = { source: 'manual', player_ids: [], team: null };
  try { imported = await api.get('/api/myroster'); } catch { /* optional */ }

  const rosterIds = imported.player_ids?.length
    ? [...imported.player_ids]
    : JSON.parse(localStorage.getItem('gridiron.myroster') || '[]');
  const fromEspn = imported.source === 'espn' && imported.player_ids?.length > 0;
  const save = () => {
    if (!fromEspn) localStorage.setItem('gridiron.myroster', JSON.stringify(rosterIds));
  };

  const render = async () => {
    const mine = rosterIds.map((id) => board.find((b) => b.player_id === id)).filter(Boolean);
    // flex:1 with min-width:0 — a select is sized by its longest option, and
    // "Amon-Ra St. Brown (WR DET)" is wider than a phone.
    const picker = el('select', { id: 'add-player', style: 'flex:1;min-width:0' },
      el('option', { value: '' }, 'Add a player…'),
      board.slice(0, 500).map((p) => el('option', { value: p.player_id },
        `${p.player_name} (${p.position} ${p.team})`)));

    const left = panel('My roster', el('div', {},
      fromEspn ? null : el('div', { style: 'display:flex;gap:8px;margin-bottom:10px' }, picker,
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
        : el('div', { class: 'note' },
            'Add players to see start/sit and playoff odds — or connect your ESPN league '
            + 'and it fills in automatically.')),
      { meta: fromEspn
          ? `${mine.length} from ESPN${imported.team ? ` · ${imported.team.team_name}` : ''}`
          : `${mine.length} players`,
        actions: fromEspn
          ? [led('on'), el('button', { class: 'btn sm ghost',
              onclick: () => { location.hash = 'espn'; } }, 'Re-sync')]
          : [el('button', { class: 'btn sm ghost',
              onclick: () => { rosterIds.length = 0; save(); render(); } }, 'Clear')] });

    const right = el('div', { class: 'grid', style: 'gap:14px;align-content:start' });
    if (mine.length >= 1) {
      const weekSel = el('select', { id: 'ls-week' },
        Array.from({ length: 18 }, (_, i) => el('option', { value: i + 1, selected: i + 1 === store.week ? 'selected' : null }, `Week ${i + 1}`)));
      right.append(panel('Start / sit', el('div', {}), { actions: [weekSel,
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
          }, 'Optimise')] }));

      const total = mine.reduce((a, p) => a + (p.proj_points || 0), 0);
      right.append(panel('Roster strength', el('div', {},
        kv('Total projected points', fmt.n(total, 0)),
        kv('Best player', mine.slice().sort((a, b) => b.proj_points - a.proj_points)[0]?.player_name || '—'),
        el('div', { style: 'margin-top:10px' },
          barChart(mine.slice().sort((a, b) => b.proj_points - a.proj_points).slice(0, 10)
            .map((p) => ({ label: p.player_name, value: p.proj_points,
              color: { QB: '#a78bfa', RB: '#4ade80', WR: '#60a5fa', TE: '#fbbf24', K: '#f472b6', DST: '#94a3b8' }[p.position] })),
            { fmt: (v) => fmt.n(v, 0) })))));
    }
    wrap.replaceChildren(el('div', { class: 'grid g2' }, left, right));
  };
  await render();
}
