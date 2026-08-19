// Small DOM + chart helpers. No dependencies: the app must run with no network.
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

export const fmt = {
  n: (v, d = 1) => (v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(d)),
  i: (v) => (v == null || Number.isNaN(v) ? '—' : Math.round(Number(v)).toLocaleString()),
  pct: (v, d = 1) => (v == null || Number.isNaN(v) ? '—' : `${(Number(v) * 100).toFixed(d)}%`),
  money: (v) => (v == null ? '—' : `$${Number(v).toFixed(0)}`),
  signed: (v, d = 1) => (v == null ? '—' : `${v > 0 ? '+' : ''}${Number(v).toFixed(d)}`),
  time: (ts) => new Date(ts * 1000).toLocaleTimeString(),
};

export function toast(msg, isError = false) {
  const node = $('#toast');
  if (!node) return;
  node.textContent = msg;
  node.classList.toggle('err', !!isError);
  node.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => node.classList.remove('show'), 4200);
}

// ---------------------------------------------------------------- API client
export const api = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
    return r.json();
  },
};

// ------------------------------------------------------------------- tables
/**
 * Sortable table. `cols` entries: {key, label, fmt, cls, width, sortable}
 */
export function table(rows, cols, opts = {}) {
  const state = { key: opts.sortKey || null, desc: opts.sortDesc !== false };
  const wrap = el('div', { class: 'tbl-wrap' });

  const render = () => {
    let data = [...rows];
    if (state.key) {
      data.sort((a, b) => {
        const x = a[state.key], y = b[state.key];
        if (x == null) return 1;
        if (y == null) return -1;
        const cmp = typeof x === 'number' ? x - y : String(x).localeCompare(String(y));
        return state.desc ? -cmp : cmp;
      });
    }
    const headCells = cols.map((c) => {
      const arrow = state.key === c.key ? (state.desc ? ' ↓' : ' ↑') : '';
      return el('th', {
        class: c.cls,
        style: c.width ? `width:${c.width}` : null,
        onclick: () => {
          if (c.sortable === false) return;
          state.desc = state.key === c.key ? !state.desc : true;
          state.key = c.key;
          render();
        },
      }, c.label + arrow);
    });
    const thead = el('thead', {}, el('tr', {}, headCells));

    const tbody = el('tbody', {}, data.map((row) => {
      const tr = el('tr', { class: opts.onRow ? 'clickable' : null },
        cols.map((c) => el('td', { class: c.cls }, c.fmt ? c.fmt(row[c.key], row) : row[c.key] ?? '—')));
      if (opts.onRow) tr.addEventListener('click', () => opts.onRow(row));
      return tr;
    }));

    wrap.replaceChildren(el('table', {}, thead, tbody));
    if (!data.length) wrap.replaceChildren(el('div', { class: 'empty' }, opts.empty || 'No rows'));
  };
  render();
  return wrap;
}

export const posTag = (p) => el('span', { class: `pos ${p}` }, p);

export function bar(value, max, label) {
  const pct = Math.max(0, Math.min(1, max ? value / max : 0));
  return el('div', { style: 'display:flex;gap:8px;align-items:center' },
    el('div', { class: 'bar' }, el('span', { style: `width:${(pct * 100).toFixed(1)}%` })),
    label != null ? el('span', { class: 'num dim' }, label) : null);
}

// ------------------------------------------------------------------- charts
const SVG = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs = {}) => {
  const n = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) n.setAttribute(k, v);
  return n;
};

/** Sparkline / line chart from an array of numbers. */
export function sparkline(values, opts = {}) {
  const w = opts.width || 200, h = opts.height || 44, pad = 2;
  const svg = svgEl('svg', { width: '100%', height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'none', class: 'spark' });
  const vals = (values || []).filter((v) => v != null && !Number.isNaN(v));
  if (vals.length < 2) {
    svg.append(svgEl('line', { x1: 0, y1: h / 2, x2: w, y2: h / 2, stroke: '#2a3a50', 'stroke-width': 1 }));
    return svg;
  }
  const lo = opts.min ?? Math.min(...vals), hi = opts.max ?? Math.max(...vals);
  const span = hi - lo || 1;
  const x = (i) => pad + (i / (vals.length - 1)) * (w - 2 * pad);
  const y = (v) => h - pad - ((v - lo) / span) * (h - 2 * pad);
  const d = vals.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(2)},${y(v).toFixed(2)}`).join('');
  const rising = vals[vals.length - 1] >= vals[0];
  const color = opts.color || (rising ? '#48bb78' : '#f56565');
  if (opts.fill !== false) {
    svg.append(svgEl('path', {
      d: `${d}L${x(vals.length - 1)},${h}L${x(0)},${h}Z`, fill: color, opacity: 0.12, stroke: 'none' }));
  }
  svg.append(svgEl('path', { d, fill: 'none', stroke: color, 'stroke-width': opts.strokeWidth || 1.6,
    'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke' }));
  return svg;
}

/** Histogram from {counts, edges}. */
export function histogram(dist, opts = {}) {
  const w = opts.width || 460, h = opts.height || 150, pad = { l: 30, r: 8, t: 8, b: 20 };
  const svg = svgEl('svg', { width: '100%', height: h, viewBox: `0 0 ${w} ${h}` });
  const counts = dist?.counts || [], edges = dist?.edges || [];
  if (!counts.length) return svg;
  const maxC = Math.max(...counts) || 1;
  const iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
  const bw = iw / counts.length;
  counts.forEach((c, i) => {
    const bh = (c / maxC) * ih;
    svg.append(svgEl('rect', {
      x: pad.l + i * bw + 0.5, y: pad.t + ih - bh, width: Math.max(bw - 1.5, 1), height: bh,
      fill: opts.color || '#4fd1c5', opacity: 0.75, rx: 1.5 }));
  });
  // axis labels at both ends and the middle
  [0, Math.floor(edges.length / 2), edges.length - 1].forEach((i) => {
    const t = svgEl('text', { x: pad.l + (i / (edges.length - 1)) * iw, y: h - 5,
      fill: '#6b7f96', 'font-size': 10, 'text-anchor': i === 0 ? 'start' : i === edges.length - 1 ? 'end' : 'middle' });
    t.textContent = Number(edges[i]).toFixed(0);
    svg.append(t);
  });
  if (opts.marker != null) {
    const lo = edges[0], hi = edges[edges.length - 1];
    const mx = pad.l + ((opts.marker - lo) / (hi - lo)) * iw;
    if (mx >= pad.l && mx <= pad.l + iw) {
      svg.append(svgEl('line', { x1: mx, y1: pad.t, x2: mx, y2: pad.t + ih,
        stroke: '#ecc94b', 'stroke-width': 1.5, 'stroke-dasharray': '3,3' }));
    }
  }
  return svg;
}

/** Horizontal bar chart from [{label, value, color}] */
export function barChart(items, opts = {}) {
  const max = Math.max(...items.map((i) => Math.abs(i.value)), 1e-9);
  return el('div', { style: 'display:flex;flex-direction:column;gap:6px' },
    items.map((it) => el('div', { style: 'display:grid;grid-template-columns:110px 1fr 62px;gap:8px;align-items:center' },
      el('span', { class: 'dim', style: 'font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }, it.label),
      el('div', { class: 'bar', style: 'height:9px' },
        el('span', { style: `width:${(Math.abs(it.value) / max * 100).toFixed(1)}%;background:${it.color || '#4fd1c5'}` })),
      el('span', { class: 'num' }, opts.fmt ? opts.fmt(it.value) : fmt.n(it.value)))));
}

export function stat(label, value, sub) {
  return el('div', { class: 'stat' },
    el('span', { class: 'l' }, label),
    el('span', { class: 'v' }, value),
    sub ? el('span', { class: 's' }, sub) : null);
}

export function kv(label, value) {
  return el('div', { class: 'kv' }, el('span', { class: 'dim' }, label), el('b', {}, value));
}
