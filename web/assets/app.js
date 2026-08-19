// Application shell: routing, global actions, status chips.
import { $, $$, api, el, fmt, toast } from './ui.js';
import { dashboard, myLeague, projections, settings, store } from './views1.js';
import { draft, games, live, markets } from './views2.js';

const VIEWS = {
  dashboard: { title: 'Dashboard', render: dashboard },
  projections: { title: 'Projections', render: projections },
  draft: { title: 'Draft Room', render: draft },
  games: { title: 'Games', render: games },
  markets: { title: 'Markets', render: markets },
  live: { title: 'Live Tape', render: live },
  league: { title: 'My League', render: myLeague },
  settings: { title: 'Settings', render: settings },
};

async function show(name) {
  const view = VIEWS[name] || VIEWS.dashboard;
  $$('#nav button').forEach((b) => b.classList.toggle('active', b.dataset.view === name));
  $('#crumb').textContent = view.title;
  location.hash = name;
  const root = $('#view');
  root.replaceChildren(el('div', { class: 'empty' }, el('span', { class: 'loading' }), ' loading'));
  try {
    await view.render(root);
  } catch (err) {
    root.replaceChildren(el('div', { class: 'card' },
      el('h3', {}, 'Something went wrong'),
      el('div', { class: 'note' }, String(err)),
      el('button', { class: 'btn', style: 'margin-top:10px', onclick: () => show(name) }, 'Retry')));
  }
}

let wasBuilding = false;

async function refreshChips() {
  try {
    const s = await api.get('/api/status');
    store.status = s;
    store.league = s.league;
    $('#brand-season').textContent = `${s.season} season`;
    $('#chip-backend').textContent = s.compute.backend;
    $('#chip-mode').textContent = `trading: ${s.trading_mode}${s.risk?.kill_switch ? ' · HALTED' : ''}`;

    const building = s.build?.building;
    if (building) {
      $('#chip-built').textContent = `simulating… (started ${s.build.started})`;
    } else if (s.build?.error) {
      $('#chip-built').textContent = `build failed — see console`;
    } else {
      $('#chip-built').textContent = s.meta?.built_at
        ? `built ${s.meta.built_at} · ${fmt.i(s.meta.n_sims)} sims`
        : 'not built yet — press Rebuild';
    }
    banner(building, s);

    // Re-render once the first simulation lands so empty views fill in.
    if (wasBuilding && !building) {
      toast('Simulation ready');
      show((location.hash || '#dashboard').slice(1));
    }
    wasBuilding = !!building;
  } catch (err) {
    $('#chip-built').textContent = 'server unreachable';
  }
}

function banner(building, s) {
  let node = $('#banner');
  if (!building && !s.build?.error) { if (node) node.remove(); return; }
  if (!node) {
    node = el('div', { id: 'banner', class: 'card',
      style: 'margin-bottom:14px;border-color:#2c7a7b' });
    $('#view').prepend(node);
  }
  node.replaceChildren(building
    ? el('div', {}, el('span', { class: 'loading' }),
        ' Running the first full simulation — the draft board and projections work now; '
        + 'games, start/sit and league odds appear when it finishes.')
    : el('div', { class: 'bad' }, `Build failed: ${s.build.error}`));
}

function wire() {
  $$('#nav button').forEach((b) => b.addEventListener('click', () => show(b.dataset.view)));
  $('#drawer-close').addEventListener('click', () => $('#drawer').classList.remove('open'));
  $('#drawer').addEventListener('click', (e) => {
    if (e.target.id === 'drawer') $('#drawer').classList.remove('open');
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') $('#drawer').classList.remove('open');
  });

  $('#btn-refresh').addEventListener('click', async (e) => {
    e.target.disabled = true; e.target.textContent = 'refreshing…';
    try {
      const r = await api.post('/api/refresh');
      const failed = Object.entries(r.steps).filter(([, v]) => v.startsWith('failed'));
      toast(failed.length ? `Refreshed with ${failed.length} source(s) unavailable` : `Data refreshed in ${r.seconds}s`,
        failed.length > 0);
    } catch (err) { toast(String(err), true); }
    e.target.disabled = false; e.target.textContent = 'Refresh data';
    refreshChips();
  });

  $('#btn-rebuild').addEventListener('click', async (e) => {
    e.target.disabled = true; e.target.textContent = 'simulating…';
    try {
      const r = await api.post('/api/rebuild', { n_sims: 5000 });
      toast(r.started ? 'Rebuilding in the background…' : r.detail);
    } catch (err) { toast(String(err), true); }
    e.target.disabled = false; e.target.textContent = 'Rebuild sims';
    refreshChips();
  });
}

wire();
refreshChips();
setInterval(refreshChips, 4000);
show((location.hash || '#dashboard').slice(1));
