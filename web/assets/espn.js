// ESPN league: import your real roster, and score this model against theirs.
import { api, el, fmt, gauge, kv, led, metric, panel, posTag, table, toast } from './ui.js';

const POS_COLOR = { QB: '#c4a2fc', RB: '#5eead4', WR: '#7dd3fc', TE: '#fcd34d', K: '#f0abfc', DST: '#94a3b8' };

export async function espn(root) {
  const wrap = el('div', {});
  root.replaceChildren(wrap);

  const status = await api.get('/api/espn/status').catch((e) => ({ error: String(e) }));

  if (!status.configured || !status.connected) {
    wrap.replaceChildren(setupPanel(status, root));
    return;
  }

  const info = el('div', { class: 'grid g4', style: 'margin-bottom:12px' },
    panel('League', gauge('name', status.name || `#${status.league_id}`,
      `${status.teams} teams · ${status.scoring_type || 'standard'}`),
      { actions: [led('on')] }),
    panel('Season', gauge('year', String(status.year), `week ${status.current_week || '—'}`)),
    panel('Access', gauge('type', status.private ? 'PRIVATE' : 'PUBLIC',
      status.private ? 'authenticated with cookies' : 'no cookies needed')),
    panel('Actions', el('div', { style: 'display:flex;flex-direction:column;gap:6px' },
      el('button', { class: 'btn primary', onclick: (e) => runSync(e, root) }, 'Sync my league'),
      el('button', { class: 'btn ghost sm', onclick: (e) => runCompare(e, body) }, 'Compare projections'),
      el('button', { class: 'btn ghost sm', onclick: (e) => runScorecard(e, body) }, 'Score vs actuals'))));

  const body = el('div', { class: 'grid', style: 'gap:12px' },
    panel('Start here', el('div', { class: 'note' },
      el('p', { style: 'margin:0 0 8px' },
        'Sync my league pulls your settings, roster, completed draft and schedule across '
        + 'in one go, so nothing has to be entered twice. Pick your team below first.'),
      el('p', { style: 'margin:0 0 8px' },
        'Compare projections lines up every player both boards know about and shows where '
        + 'they disagree — that is your edge over league-mates reading ESPN’s numbers.'),
      el('p', { style: 'margin:0' },
        'Score vs actuals is the honest test: both projections measured against what '
        + 'actually happened, on the same players and weeks.'))));

  wrap.replaceChildren(info, body);
  loadLeague(body, root);
}

function setupPanel(status, root) {
  const field = (label, id, ph) => el('label', { class: 'field', style: 'margin-bottom:10px' },
    label, el('input', { id, placeholder: ph, autocomplete: 'off', spellcheck: 'false' }));

  return el('div', { class: 'grid g2' },
    panel('Connect your ESPN league',
      el('div', {},
        status.detail ? el('div', { class: 'note', style: 'margin-bottom:12px;color:var(--warn)' },
          status.detail) : null,
        field('League ID', 'espn-id', 'from the URL: leagueId=XXXXXXX'),
        field('espn_s2 cookie (private leagues)', 'espn-s2', 'AEB...long string...'),
        field('SWID cookie (private leagues)', 'espn-swid', '{XXXXXXXX-XXXX-...}'),
        field('Season year (blank = current)', 'espn-year', '2026'),
        el('button', {
          class: 'btn primary', style: 'margin-top:6px',
          onclick: async (e) => {
            e.target.disabled = true; e.target.textContent = 'connecting';
            try {
              const r = await api.post('/api/espn/credentials', {
                league_id: Number(document.getElementById('espn-id').value),
                espn_s2: document.getElementById('espn-s2').value,
                swid: document.getElementById('espn-swid').value,
                year: Number(document.getElementById('espn-year').value) || null,
              });
              if (r.ok) { toast(`connected to ${r.name || 'your league'}`); espn(root); }
              else { toast(r.detail || 'could not connect', true); }
            } catch (err) { toast(String(err), true); }
            e.target.disabled = false; e.target.textContent = 'Connect';
          },
        }, 'Connect')),
      { meta: status.library_installed ? '' : 'pip install espn_api' }),

    panel('Finding your cookies',
      el('div', { class: 'note' },
        el('p', { style: 'margin:0 0 10px' },
          'A public league needs only the league ID, which is in the URL when you view '
          + 'your league on fantasy.espn.com.'),
        el('p', { style: 'margin:0 0 10px' },
          'A private league needs two cookies from a signed-in browser session. In Chrome '
          + 'or Edge: open your league, press F12, then Application → Storage → Cookies → '
          + 'https://fantasy.espn.com. Copy the values of espn_s2 and SWID. In Firefox the '
          + 'same list is under Storage.'),
        el('p', { style: 'margin:0 0 10px' },
          'Keep the braces on SWID. Both are stored only in data/user/espn_credentials.json '
          + 'with owner-only permissions, and are sent nowhere except ESPN.'),
        el('p', { style: 'margin:0' },
          'They expire every few months. If a connection that used to work starts failing, '
          + 'copy fresh values.'))));
}

async function loadLeague(body, root) {
  try {
    const d = await api.get('/api/espn/league');
    const teams = d.teams || [];
    const status = await api.get('/api/espn/status').catch(() => ({}));

    const picker = el('select', { id: 'espn-team' },
      el('option', { value: '' }, 'Which team is yours?'),
      teams.map((t) => el('option', {
        value: t.team_id,
        selected: status.team_id && Number(status.team_id) === Number(t.team_id) ? 'selected' : null,
      }, `${t.team_name}${t.owner ? ` — ${t.owner}` : ''}`)));

    body.append(panel('Your team',
      el('div', {},
        el('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap' },
          picker,
          el('button', { class: 'btn primary', onclick: (e) => runSync(e, root) }, 'Sync my league')),
        el('div', { class: 'note', style: 'margin-top:9px' },
          'Syncing imports your league’s scoring and roster slots, your players, your '
          + 'completed draft picks and your weekly matchups. My League and the Draft Room '
          + 'then use them directly — no second setup.')),
      { meta: 'one-time setup' }));

    body.append(panel('League teams',
      table(teams, [
        { key: 'standing', label: '#', cls: 'num' },
        { key: 'team_name', label: 'Team', cls: 'name' },
        { key: 'owner', label: 'Owner', cls: 'dim' },
        { key: 'wins', label: 'W', cls: 'num' },
        { key: 'losses', label: 'L', cls: 'num' },
        { key: 'points_for', label: 'PF', cls: 'num', fmt: (v) => fmt.n(v, 1) },
        { key: 'points_against', label: 'PA', cls: 'num dim', fmt: (v) => fmt.n(v, 1) },
        { key: 'roster_size', label: 'Roster', cls: 'num dim' },
      ], { sortKey: 'standing', sortDesc: false, empty: 'No teams returned' }),
      { flush: true, meta: `${teams.length} teams` }));
  } catch (err) {
    body.append(panel('League', el('div', { class: 'note' }, String(err))));
  }
}

async function runCompare(evt, body) {
  const btn = evt.target;
  btn.disabled = true; btn.textContent = 'comparing';
  try {
    const d = await api.post('/api/espn/compare', { min_points: 20, top_n: 15 });
    const a = d.agreement || {};

    const cols = [
      { key: 'player_name', label: 'Player', cls: 'name',
        fmt: (v, r) => el('span', {}, posTag(r.position), ' ', v) },
      { key: 'team', label: 'Tm', cls: 'dim' },
      { key: 'proj_ppg', label: 'Gridiron', cls: 'num', fmt: (v) => fmt.n(v, 1) },
      { key: 'espn_proj_avg', label: 'ESPN', cls: 'num', fmt: (v) => fmt.n(v, 1) },
      { key: 'delta_ppg', label: 'Δ ppg', cls: 'num',
        fmt: (v) => el('span', { class: v > 0 ? 'good' : 'bad' }, fmt.signed(v, 1)) },
      { key: 'fantasy_team', label: 'Rostered by', cls: 'dim',
        fmt: (v) => v || el('span', { class: 'dim' }, 'free agent') },
    ];

    body.replaceChildren(
      el('div', { class: 'grid g4' },
        panel('Compared', gauge('players', fmt.i(d.n_compared), 'on both boards')),
        panel('Agreement', gauge('correlation', fmt.n(a.correlation, 3),
          `rank corr ${fmt.n(a.rank_correlation, 3)}`, a.correlation)),
        panel('Typical gap', gauge('median', `${fmt.n(a.median_abs_diff_ppg, 2)}`,
          `mean ${fmt.n(a.mean_abs_diff_ppg, 2)} ppg · max ${fmt.n(a.largest_gap_ppg, 1)}`)),
        panel('Lean', gauge('bias', fmt.signed(a.mean_bias_ppg, 2),
          `we are higher on ${fmt.pct(a.gridiron_higher_pct, 0)} of players`))),

      el('div', { class: 'grid g2', style: 'margin-top:12px' },
        panel('We are higher than ESPN',
          table(d.gridiron_higher || [], cols, { sortKey: 'delta_ppg' }),
          { flush: true, meta: 'candidates to start or buy' }),
        panel('ESPN is higher than us',
          table(d.espn_higher || [], cols, { sortKey: 'delta_ppg', sortDesc: false }),
          { flush: true, meta: 'candidates to fade or sell' })),

      el('div', { style: 'margin-top:12px' },
        panel('Every compared player', table(d.all || [], cols, { sortKey: 'delta_ppg' }),
          { flush: true, meta: `${(d.all || []).length} shown` })),

      el('div', { style: 'margin-top:12px' },
        panel('Reading this', el('div', { class: 'note' },
          'A gap is not a claim that ESPN is wrong. ESPN’s numbers are what the rest of '
          + 'your league sees, so a disagreement is a mispricing inside your league whether '
          + 'or not it turns out to be the better forecast. Use Score vs actuals once games '
          + 'have been played to find out which board is actually more accurate.'))));
    toast(`${d.n_compared} players compared`);
  } catch (err) { toast(String(err), true); }
  btn.disabled = false; btn.textContent = 'Compare projections';
}

async function runScorecard(evt, body) {
  const btn = evt.target;
  btn.disabled = true; btn.textContent = 'scoring';
  try {
    const d = await api.post('/api/espn/scorecard', { n_sims: 4000 });
    if (!d.scored) {
      body.replaceChildren(panel('Not scorable yet', el('div', { class: 'note' },
        d.note || 'No completed weeks yet.')));
      btn.disabled = false; btn.textContent = 'Score vs actuals';
      return;
    }
    const g = d.gridiron || {}, e = d.espn || {};
    const winner = d.gridiron_win_rate > d.espn_win_rate;

    const row = (label, gv, ev, lowerIsBetter = true) => {
      const gBetter = lowerIsBetter ? gv < ev : gv > ev;
      return el('div', { class: 'metric' },
        el('span', { class: 'k' }, label),
        el('span', { class: 'v' },
          el('span', { class: gBetter ? 'good' : 'dim' }, fmt.n(gv, 3)),
          el('span', { class: 'dim' }, '  vs  '),
          el('span', { class: !gBetter ? 'good' : 'dim' }, fmt.n(ev, 3))));
    };

    body.replaceChildren(
      el('div', { class: 'grid g4' },
        panel('Scored', gauge('player-weeks', fmt.i(d.scored),
          `weeks ${(d.weeks || []).join(', ') || '—'}`)),
        panel('Gridiron closer', gauge('win rate', fmt.pct(d.gridiron_win_rate, 1),
          `ties ${fmt.pct(d.ties, 1)}`, d.gridiron_win_rate)),
        panel('ESPN closer', gauge('win rate', fmt.pct(d.espn_win_rate, 1), '',
          d.espn_win_rate)),
        panel('Accuracy edge', gauge('mae', fmt.signed(d.mae_edge, 3),
          d.mae_edge > 0 ? 'points better per player-week' : 'points worse per player-week'))),

      el('div', { class: 'grid g2', style: 'margin-top:12px' },
        panel('Head to head', el('div', {},
          el('div', { class: 'note', style: 'margin-bottom:10px' },
            'Gridiron vs ESPN. Lower is better for error and bias; higher for correlation.'),
          row('mean absolute error', g.mae, e.mae),
          row('root mean squared error', g.rmse, e.rmse),
          row('bias', Math.abs(g.bias), Math.abs(e.bias)),
          row('correlation with actual', g.correlation, e.correlation, false)),
          { actions: [led(winner ? 'on' : 'off')] }),
        panel('Verdict', el('div', { class: 'note', style: 'font-size:13px;line-height:1.7' },
          d.verdict))),

      (d.biggest_misses || []).length ? el('div', { style: 'margin-top:12px' },
        panel('Our biggest misses', table(d.biggest_misses, [
          { key: 'player_name', label: 'Player', cls: 'name',
            fmt: (v, r) => el('span', {}, posTag(r.position || 'WR'), ' ', v) },
          { key: 'week', label: 'Wk', cls: 'num dim' },
          { key: 'gridiron_proj', label: 'Ours', cls: 'num', fmt: (v) => fmt.n(v, 1) },
          { key: 'espn_week_proj', label: 'ESPN', cls: 'num', fmt: (v) => fmt.n(v, 1) },
          { key: 'espn_week_actual', label: 'Actual', cls: 'num', fmt: (v) => fmt.n(v, 1) },
          { key: 'gridiron_err', label: 'Our err', cls: 'num',
            fmt: (v) => el('span', { class: 'bad' }, fmt.n(v, 1)) },
          { key: 'espn_err', label: 'ESPN err', cls: 'num dim', fmt: (v) => fmt.n(v, 1) },
        ], { sortKey: 'gridiron_err' }), { flush: true,
          meta: 'where we were furthest off — worth understanding' })) : null);
    toast(d.verdict.slice(0, 80));
  } catch (err) { toast(String(err), true); }
  btn.disabled = false; btn.textContent = 'Score vs actuals';
}

async function runSync(evt, root) {
  const btn = evt.target;
  btn.disabled = true; btn.textContent = 'syncing';
  const picked = document.getElementById('espn-team');
  const teamId = picked && picked.value ? Number(picked.value) : null;
  try {
    const d = await api.post('/api/espn/sync', {
      settings: true, roster: true, draft: true, schedule: true, team_id: teamId,
    });
    const imp = d.imported || {};
    const parts = [];
    if (imp.settings) parts.push(`${imp.settings.teams}-team ${imp.settings.scoring_preset}`);
    if (imp.roster) parts.push(`${imp.roster.matched}/${imp.roster.espn_players} players`);
    if (imp.draft && imp.draft.recorded) parts.push(`${imp.draft.recorded} draft picks`);
    if (imp.schedule) parts.push(`${(imp.schedule || []).length} weeks`);
    toast(`imported ${parts.join(' · ') || 'nothing new'}`,
          (d.warnings || []).length > 0);

    const detail = el('div', { class: 'grid g2' },
      panel('Imported', el('div', {},
        imp.settings ? kv('league settings',
          `${imp.settings.teams} teams · ${imp.settings.scoring_preset}`) : null,
        imp.settings ? kv('roster slots',
          Object.entries(imp.settings.roster).map(([k, v]) => `${v}${k}`).join(' ')) : null,
        imp.roster ? kv('your team', imp.roster.team ? imp.roster.team.team_name : '—') : null,
        imp.roster ? kv('players matched',
          `${imp.roster.matched} of ${imp.roster.espn_players}`) : null,
        imp.draft ? kv('draft picks recorded', String(imp.draft.recorded ?? 0)) : null,
        el('div', { class: 'note', style: 'margin-top:10px' },
          'League settings changed, so rebuild simulations to apply them.'),
        el('button', {
          class: 'btn primary sm', style: 'margin-top:8px',
          onclick: async (e) => {
            e.target.disabled = true; e.target.textContent = 'rebuilding';
            try { await api.post('/api/rebuild', { n_sims: 5000 }); toast('rebuilding in the background'); }
            catch (err) { toast(String(err), true); }
          },
        }, 'Rebuild now')),
        { actions: [led('on')] }),
      panel('Your schedule',
        table(imp.schedule || [], [
          { key: 'week', label: 'Wk', cls: 'num' },
          { key: 'opponent', label: 'Opponent', cls: 'name' },
          { key: 'my_score', label: 'Score', cls: 'num', fmt: (v) => (v ? fmt.n(v, 1) : '—') },
          { key: 'outcome', label: 'Result', fmt: (v) => el('span',
            { class: v === 'W' ? 'good' : v === 'L' ? 'bad' : 'dim' }, v || '—') },
        ], { sortKey: 'week', sortDesc: false, empty: 'No schedule returned' }),
        { flush: true }));

    const warn = (d.warnings || []).length
      ? panel('Warnings', el('div', { class: 'note' },
          (d.warnings || []).map((w) => el('p', { style: 'margin:0 0 6px' }, w))))
      : null;

    const host = document.querySelector('#view .grid');
    if (host) host.replaceChildren(detail, ...(warn ? [warn] : []));
  } catch (err) { toast(String(err), true); }
  btn.disabled = false; btn.textContent = 'Sync my league';
}
