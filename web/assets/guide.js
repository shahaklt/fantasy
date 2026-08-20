// The stats guide: a reference you can actually use mid-draft.
import { $, el, panel } from './ui.js';
import { CONCEPTS, DRAFT_PLAYBOOK, GROUPS, SEASON_PLAYBOOK, SEASON_PLAYBOOK as _s, STATS } from './guide-data.js';

const SECTIONS = [
  { id: 'start', label: 'Start here', grp: 'Orientation' },
  { id: 'draftday', label: 'Draft day', grp: 'Orientation' },
  { id: 'inseason', label: 'In season', grp: 'Orientation' },
  ...GROUPS.map((g) => ({ id: `g-${g.replace(/\s+/g, '')}`, label: g, grp: 'Stat reference' })),
];

export async function guide(root) {
  const state = { q: '' };
  const search = el('input', {
    type: 'search', placeholder: 'Filter stats…', value: state.q,
    oninput: (e) => { state.q = e.target.value.trim().toLowerCase(); render(); },
  });

  const toc = el('nav', { class: 'guide-toc' });
  let lastGrp = null;
  SECTIONS.forEach((s) => {
    if (s.grp !== lastGrp) { toc.append(el('div', { class: 'grp' }, s.grp)); lastGrp = s.grp; }
    toc.append(el('a', { href: `#${s.id}`, 'data-sec': s.id,
      onclick: (e) => {
        e.preventDefault();
        $(`#${s.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      } }, s.label));
  });

  const content = el('div', { class: 'guide grid', style: 'gap:14px' });
  const layout = el('div', { class: 'guide-layout' },
    el('div', { class: 'guide-sticky' }, panel('Contents', toc, { flush: true })),
    el('div', {},
      el('div', { class: 'controls' }, search,
        el('span', { class: 'note' },
          'Every number this app shows, what it means, and what to do about it.')),
      content));
  root.replaceChildren(layout);

  function matches(s) {
    if (!state.q) return true;
    return `${s.key} ${s.name} ${s.body} ${s.read || ''} ${s.where}`.toLowerCase().includes(state.q);
  }

  function statEntry(s) {
    return el('article', { class: 'stat-entry' },
      el('div', { class: 'stat-head' },
        el('code', { class: 'stat-key' }, s.key),
        el('h3', {}, s.name),
        el('span', { class: 'stat-where' }, s.where)),
      el('div', { class: 'stat-body' },
        el('p', { html: s.body }),
        s.read ? el('div', { class: 'stat-read' }, el('b', {}, 'How to read it'),
          el('span', { html: s.read })) : null,
        s.eg ? el('div', { class: 'stat-eg' }, el('b', {}, '→ '), el('span', { html: s.eg })) : null));
  }

  function render() {
    const blocks = [];

    if (!state.q) {
      blocks.push(el('div', { id: 'start' },
        panel('Start here — four ideas the rest depends on',
          el('div', { class: 'guide' },
            CONCEPTS.map((c, i) => el('div', {
              style: i ? 'margin-top:16px;padding-top:16px;border-top:1px solid var(--line)' : '' },
              el('h2', {}, c.h), el('p', { html: c.p })))))));

      blocks.push(el('div', { id: 'draftday' },
        panel('Draft day — the order to do things in',
          el('div', { class: 'playbook' },
            DRAFT_PLAYBOOK.map((s) => el('div', { class: 'playbook-step' },
              el('div', {}, el('h3', {}, s.h), el('p', { html: s.p }))))),
          { meta: `${DRAFT_PLAYBOOK.length} steps` })));

      blocks.push(el('div', { id: 'inseason' },
        panel('In season — the weekly loop',
          el('div', { class: 'playbook' },
            SEASON_PLAYBOOK.map((s) => el('div', { class: 'playbook-step' },
              el('div', {}, el('h3', {}, s.h), el('p', { html: s.p }))))),
          { meta: `${SEASON_PLAYBOOK.length} steps` })));
    }

    let shown = 0;
    GROUPS.forEach((g) => {
      const rows = STATS.filter((s) => s.group === g && matches(s));
      if (!rows.length) return;
      shown += rows.length;
      blocks.push(el('div', { id: `g-${g.replace(/\s+/g, '')}` },
        panel(g, el('div', {}, rows.map(statEntry)),
          { meta: `${rows.length} ${rows.length === 1 ? 'stat' : 'stats'}` })));
    });

    if (!shown) {
      blocks.push(panel('No match', el('div', { class: 'empty' },
        `Nothing matches "${state.q}".`)));
    }
    content.replaceChildren(...blocks);
    wireScrollSpy();
  }

  function wireScrollSpy() {
    const links = [...toc.querySelectorAll('a')];
    const targets = links.map((a) => document.getElementById(a.dataset.sec)).filter(Boolean);
    if (!targets.length) return;
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        links.forEach((a) => a.classList.toggle('on', a.dataset.sec === e.target.id));
      });
    }, { rootMargin: '-10% 0px -80% 0px' });
    targets.forEach((t) => io.observe(t));
  }

  render();
}
