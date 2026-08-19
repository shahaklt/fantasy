// Application shell: routing, keyboard control, live machine readout.
import { $, $$, api, el, fmt, led, toast } from './ui.js';
import { dashboard, myLeague, projections, settings, store } from './views1.js';
import { draft, games, live, markets } from './views2.js';
import { guide } from './guide.js';
import { espn } from './espn.js';

const VIEWS = {
  dashboard:   { title: 'Dashboard',   render: dashboard,   key: '1' },
  projections: { title: 'Projections', render: projections, key: '2' },
  draft:       { title: 'Draft Room',  render: draft,       key: '3' },
  games:       { title: 'Games',       render: games,       key: '4' },
  league:      { title: 'My League',   render: myLeague,    key: '5' },
  espn:        { title: 'ESPN League', render: espn,        key: '0' },
  markets:     { title: 'Markets',     render: markets,     key: '6' },
  live:        { title: 'Live Tape',   render: live,        key: '7' },
  guide:       { title: 'Stats Guide', render: guide,       key: '8' },
  settings:    { title: 'Settings',    render: settings,    key: '9' },
};

let current = 'dashboard';

async function show(name) {
  const view = VIEWS[name] || VIEWS.dashboard;
  current = VIEWS[name] ? name : 'dashboard';
  $$('#nav button').forEach((b) => b.classList.toggle('active', b.dataset.view === current));
  $('#crumb').textContent = view.title;
  location.hash = current;
  const root = $('#view');
  root.replaceChildren(el('div', { class: 'empty' }, 'loading…'));
  try {
    await view.render(root);
  } catch (err) {
    root.replaceChildren(el('div', { class: 'panel' },
      el('div', { class: 'panel-head' }, el('div', { class: 'panel-title' }, 'Error')),
      el('div', { class: 'panel-body' },
        el('div', { class: 'note' }, String(err)),
        el('button', { class: 'btn', style: 'margin-top:10px', onclick: () => show(current) }, 'Retry'))));
  }
}

// -------------------------------------------------------------- readout strip
let wasBuilding = false;

function readoutCell(label, value, cls, ledState) {
  return el('div', { class: `readout-cell ${cls || ''}` },
    ledState ? led(ledState) : null,
    el('span', {}, label), el('b', {}, value));
}

async function refreshStatus() {
  const strip = $('#readout');
  try {
    const s = await api.get('/api/status');
    store.status = s;
    store.league = s.league;

    const building = !!s.build?.building;
    const meta = s.meta || {};

    $('#brand-season').textContent = `${s.season} SEASON`;
    $('#chip-backend').textContent = s.compute.gpu ? 'CUDA' : 'CPU';
    $('#chip-mode').textContent = s.trading_mode + (s.risk?.kill_switch ? ' · HALT' : '');
    $('#chip-built').textContent = building ? 'simulating' : (meta.built_at || '—').slice(-8);

    strip.replaceChildren(
      readoutCell('sim', building ? 'BUILDING' : (s.sims_ready ? 'READY' : 'STALE'),
        building ? '' : 'hi', building ? 'busy' : (s.sims_ready ? 'on' : 'off')),
      readoutCell('runs', fmt.i(meta.n_sims || 0)),
      readoutCell('players', fmt.i(meta.players || 0)),
      readoutCell('engine', s.compute.backend),
      readoutCell('sched', s.scheduler?.running ? (s.scheduler.times || []).join('/') : 'off',
        '', s.scheduler?.running ? 'on' : 'off'),
      readoutCell('trading', s.trading_mode,
        s.risk?.kill_switch ? '' : '', s.risk?.kill_switch ? 'off' : 'on'),
      readoutCell('venues',
        (s.venues || []).filter((v) => v.authenticated).length + '/' + (s.venues || []).length),
      el('div', { class: 'readout-cell', style: 'margin-left:auto;border-right:0' },
        el('span', {}, new Date().toLocaleTimeString())));

    if (building) {
      $('#view').querySelectorAll('.build-banner').forEach((n) => n.remove());
      if (!$('.build-banner')) {
        $('#view').prepend(el('div', { class: 'panel build-banner', style: 'margin-bottom:12px' },
          el('div', { class: 'panel-head' },
            el('div', { class: 'panel-title' }, 'First simulation running'),
            el('div', { class: 'panel-actions' }, led('busy'))),
          el('div', { class: 'panel-body' }, el('div', { class: 'note' },
            'The draft board and projections work now. Games, start/sit and league odds '
            + 'appear when this finishes.'))));
      }
    } else {
      $$('.build-banner').forEach((n) => n.remove());
    }

    if (wasBuilding && !building) { toast('simulation ready'); show(current); }
    wasBuilding = building;
  } catch {
    strip.replaceChildren(readoutCell('server', 'UNREACHABLE', '', 'off'));
  }
}

// ------------------------------------------------------------ command palette
const palette = {
  open: false, items: [], sel: 0,
  show() {
    this.open = true; this.sel = 0;
    $('#palette').classList.add('open');
    const input = $('#palette-input');
    input.value = ''; input.focus();
    this.build('');
  },
  hide() { this.open = false; $('#palette').classList.remove('open'); },
  async build(q) {
    const query = q.trim().toLowerCase();
    const views = Object.entries(VIEWS)
      .filter(([k, v]) => !query || v.title.toLowerCase().includes(query) || k.includes(query))
      .map(([k, v]) => ({ tag: 'view', label: v.title, hint: `⌥${v.key}`, run: () => show(k) }));

    let players = [];
    if (query.length >= 2 && store.board?.length) {
      players = store.board
        .filter((p) => p.player_name.toLowerCase().includes(query))
        .slice(0, 8)
        .map((p) => ({
          tag: p.position, label: p.player_name,
          hint: `${p.team} · ${fmt.n(p.proj_points, 0)}pts`,
          run: () => import('./views1.js').then((m) => m.playerDrawer(p.player_id)),
        }));
    }
    this.items = [...views, ...players];
    this.render();
  },
  render() {
    const list = $('#palette-list');
    list.replaceChildren(...this.items.map((it, i) => el('div', {
      class: `palette-item${i === this.sel ? ' sel' : ''}`,
      onclick: () => { this.hide(); it.run(); },
    }, el('span', { class: 'tag' }, it.tag), el('span', {}, it.label),
       el('span', { class: 'tag' }, it.hint || ''))));
    if (!this.items.length) list.replaceChildren(el('div', { class: 'empty' }, 'no match'));
  },
  move(d) {
    if (!this.items.length) return;
    this.sel = (this.sel + d + this.items.length) % this.items.length;
    this.render();
    $('#palette-list').children[this.sel]?.scrollIntoView({ block: 'nearest' });
  },
  choose() {
    const it = this.items[this.sel];
    if (it) { this.hide(); it.run(); }
  },
};

// --------------------------------------------------------------------- wiring
function wire() {
  $$('#nav button').forEach((b) => b.addEventListener('click', () => show(b.dataset.view)));

  const closeDrawer = () => $('#drawer').classList.remove('open');
  $('#drawer-close').addEventListener('click', closeDrawer);
  $('#drawer').addEventListener('click', (e) => { if (e.target.id === 'drawer') closeDrawer(); });

  $('#btn-palette').addEventListener('click', () => palette.show());
  $('#palette-input').addEventListener('input', (e) => palette.build(e.target.value));
  $('#palette').addEventListener('click', (e) => { if (e.target.id === 'palette') palette.hide(); });

  document.addEventListener('keydown', (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);

    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault(); palette.open ? palette.hide() : palette.show(); return;
    }
    if (palette.open) {
      if (e.key === 'Escape') { e.preventDefault(); palette.hide(); }
      if (e.key === 'ArrowDown') { e.preventDefault(); palette.move(1); }
      if (e.key === 'ArrowUp') { e.preventDefault(); palette.move(-1); }
      if (e.key === 'Enter') { e.preventDefault(); palette.choose(); }
      return;
    }
    if (e.key === 'Escape') { closeDrawer(); return; }
    // Alt+digit jumps views without stealing plain number keys from inputs.
    if (e.altKey && /^[1-9]$/.test(e.key)) {
      const hit = Object.entries(VIEWS).find(([, v]) => v.key === e.key);
      if (hit) { e.preventDefault(); show(hit[0]); }
      return;
    }
    if (!typing && e.key === '?') { e.preventDefault(); show('guide'); }
  });

  $('#btn-refresh').addEventListener('click', async (e) => {
    e.target.disabled = true; e.target.textContent = 'refreshing';
    try {
      const r = await api.post('/api/refresh');
      const failed = Object.entries(r.steps).filter(([, v]) => v.startsWith('failed'));
      toast(failed.length ? `${failed.length} source(s) unavailable` : `data refreshed in ${r.seconds}s`,
        failed.length > 0);
    } catch (err) { toast(String(err), true); }
    e.target.disabled = false; e.target.textContent = 'Refresh data';
    refreshStatus();
  });

  $('#btn-rebuild').addEventListener('click', async (e) => {
    e.target.disabled = true; e.target.textContent = 'queued';
    try {
      const r = await api.post('/api/rebuild', { n_sims: 5000 });
      toast(r.started ? 'rebuilding in the background' : r.detail);
    } catch (err) { toast(String(err), true); }
    e.target.disabled = false; e.target.textContent = 'Rebuild sims';
    refreshStatus();
  });

  window.addEventListener('hashchange', () => {
    const name = location.hash.slice(1);
    if (name && name !== current) show(name);
  });
}

wire();
refreshStatus();
setInterval(refreshStatus, 4000);
show((location.hash || '#dashboard').slice(1));
