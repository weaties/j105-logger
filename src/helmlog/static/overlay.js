/* Multi-maneuver time-aligned overlay chart (#619).
 *
 * Fetches /api/maneuvers/overlay?ids=sid:mid,... and renders stacked
 * panels (BSP, heading rate, TWA) on a shared x-axis centred at t=0
 * (head-to-wind). Each selected maneuver draws as a translucent line;
 * the best (lowest loss_percentile) is highlighted green, the worst
 * red, the rest grey. At >=15 maneuvers, auto-switches to a percentile
 * band render (p10/p25/p50/p75/p90); the user can override via toggle.
 *
 * Hand-rolled canvas matches the existing compare.js style and avoids
 * pulling in a chart library for a first landing — uPlot remains the
 * recommended path if we outgrow this (issue #619).
 */
'use strict';

const OV_AXIS_MIN = -20;
const OV_AXIS_MAX = 30;
const OV_BAND_AUTO_THRESHOLD = 15;

const OV_CHANNELS = [
  { key: 'bsp',                 label: 'Boat speed',    unit: 'kt',     decimals: 1 },
  { key: 'heading_rate_deg_s',  label: 'Heading rate',  unit: '°/s',    decimals: 1 },
  { key: 'twa',                 label: 'TWA',           unit: '°',      decimals: 0 },
];

const _ovState = {
  data: null,
  mode: 'auto',    // 'lines' | 'bands' | 'auto'
  hoverId: null,   // "sid:mid" of the currently highlighted curve
  panels: [],      // [{ channel, canvas, ctx, bbox }]
};

// ---------------------------------------------------------------------------
// Fetch + bootstrap
// ---------------------------------------------------------------------------

function _ovIdsFromUrl() {
  const params = new URLSearchParams(location.search);
  return (params.get('ids') || '').trim();
}

async function ovInit() {
  const ids = _ovIdsFromUrl();
  const panelsEl = document.getElementById('ov-panels');
  const emptyEl = document.getElementById('ov-empty');
  const noticeEl = document.getElementById('ov-notice');
  const legendEl = document.getElementById('ov-legend');
  const subtitleEl = document.getElementById('ov-subtitle');

  if (!ids) {
    emptyEl.style.display = '';
    return;
  }

  let payload;
  try {
    const r = await fetch('/api/maneuvers/overlay?ids=' + encodeURIComponent(ids));
    if (!r.ok) {
      noticeEl.textContent = 'Failed to load overlay: HTTP ' + r.status;
      noticeEl.style.display = '';
      return;
    }
    payload = await r.json();
  } catch (e) {
    noticeEl.textContent = 'Failed to load overlay: ' + e;
    noticeEl.style.display = '';
    return;
  }

  _ovState.data = payload;

  if (!payload.maneuvers || payload.maneuvers.length === 0) {
    emptyEl.style.display = '';
    if (payload.excluded_ids && payload.excluded_ids.length) {
      noticeEl.textContent =
        'Excluded (no head-to-wind): ' + payload.excluded_ids.join(', ');
      noticeEl.style.display = '';
    }
    return;
  }

  // Subtitle: count + session summary.
  const uniqueSessions = new Set(payload.maneuvers.map(m => m.session_id));
  subtitleEl.textContent =
    payload.maneuvers.length + ' maneuver' + (payload.maneuvers.length === 1 ? '' : 's') +
    ' across ' + uniqueSessions.size + ' session' + (uniqueSessions.size === 1 ? '' : 's');

  if (payload.excluded_ids && payload.excluded_ids.length) {
    noticeEl.textContent =
      payload.excluded_ids.length + ' excluded (no head-to-wind): ' +
      payload.excluded_ids.join(', ');
    noticeEl.style.display = '';
  }

  // Build one canvas per channel.
  panelsEl.innerHTML = '';
  _ovState.panels = OV_CHANNELS.map(ch => {
    const panel = document.createElement('div');
    panel.className = 'ov-panel';
    panel.innerHTML =
      '<h4>' + ch.label + ' (' + ch.unit + ')</h4>' +
      '<div class="ov-canvas-wrap"><canvas></canvas></div>';
    panelsEl.appendChild(panel);
    const canvas = panel.querySelector('canvas');
    return { channel: ch, canvas, ctx: canvas.getContext('2d'), bbox: null };
  });

  legendEl.style.display = '';

  // Hover tracking across all panels.
  _ovState.panels.forEach(p => {
    p.canvas.addEventListener('mousemove', evt => _ovOnHover(evt, p));
    p.canvas.addEventListener('mouseleave', () => { _ovState.hoverId = null; _ovRender(); });
  });

  window.addEventListener('resize', _ovRender);
  _ovRender();
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

function _ovResolveMode() {
  if (_ovState.mode !== 'auto') return _ovState.mode;
  const n = (_ovState.data && _ovState.data.maneuvers.length) || 0;
  return n >= OV_BAND_AUTO_THRESHOLD ? 'bands' : 'lines';
}

function _ovRender() {
  if (!_ovState.data) return;
  const mode = _ovResolveMode();
  _ovState.panels.forEach(p => _ovRenderPanel(p, mode));
}

function _ovRenderPanel(panel, mode) {
  const { canvas, ctx, channel } = panel;
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth;
  const cssH = canvas.clientHeight;
  if (cssW === 0 || cssH === 0) return;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const padL = 44, padR = 10, padT = 6, padB = 20;
  const plotW = cssW - padL - padR;
  const plotH = cssH - padT - padB;
  panel.bbox = { padL, padR, padT, padB, plotW, plotH };

  ctx.fillStyle = getComputedStyle(document.body).getPropertyValue('--bg-secondary') || '#1a1f26';
  ctx.fillRect(0, 0, cssW, cssH);

  const axis = _ovState.data.axis_s;
  const maneuvers = _ovState.data.maneuvers;
  const allValues = [];
  maneuvers.forEach(m => {
    const series = m[channel.key];
    if (!series) return;
    series.forEach(v => { if (v !== null && v !== undefined) allValues.push(v); });
  });
  if (!allValues.length) {
    ctx.fillStyle = getComputedStyle(document.body).getPropertyValue('--text-secondary') || '#888';
    ctx.font = '11px sans-serif';
    ctx.fillText('(no data in window)', padL + 6, padT + 12);
    return;
  }

  let yMin = Math.min(...allValues);
  let yMax = Math.max(...allValues);
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const yPad = (yMax - yMin) * 0.08;
  yMin -= yPad; yMax += yPad;

  const xToPx = x => padL + ((x - OV_AXIS_MIN) / (OV_AXIS_MAX - OV_AXIS_MIN)) * plotW;
  const yToPx = y => padT + (1 - (y - yMin) / (yMax - yMin)) * plotH;

  // Grid: vertical 0-line
  ctx.strokeStyle = 'rgba(180,180,180,0.25)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(xToPx(0), padT);
  ctx.lineTo(xToPx(0), padT + plotH);
  ctx.stroke();

  // Axis labels (x)
  ctx.fillStyle = getComputedStyle(document.body).getPropertyValue('--text-secondary') || '#888';
  ctx.font = '10px monospace';
  ctx.textAlign = 'center';
  [-20, -10, 0, 10, 20, 30].forEach(x => {
    const lbl = x === 0 ? 'HTW' : (x > 0 ? '+' + x + 's' : x + 's');
    ctx.fillText(lbl, xToPx(x), padT + plotH + 12);
  });
  ctx.textAlign = 'right';
  [yMin + (yMax - yMin) * 0.0,
   yMin + (yMax - yMin) * 0.5,
   yMin + (yMax - yMin) * 1.0].forEach(y => {
    ctx.fillText(y.toFixed(channel.decimals), padL - 4, yToPx(y) + 3);
  });

  if (mode === 'bands') {
    _ovRenderBands(ctx, panel, axis, maneuvers, channel, xToPx, yToPx);
  } else {
    _ovRenderLines(ctx, panel, axis, maneuvers, channel, xToPx, yToPx);
  }

  // Panel border
  ctx.strokeStyle = getComputedStyle(document.body).getPropertyValue('--border') || '#333';
  ctx.strokeRect(padL, padT, plotW, plotH);
}

function _ovRenderLines(ctx, panel, axis, maneuvers, channel, xToPx, yToPx) {
  if (!maneuvers.length) return;
  // Rank by loss_percentile → best (lowest) green, worst (highest) red.
  const pctls = maneuvers
    .map(m => m.loss_percentile)
    .filter(v => v !== null && v !== undefined);
  const minP = pctls.length ? Math.min(...pctls) : null;
  const maxP = pctls.length ? Math.max(...pctls) : null;

  const isBest = m => m.loss_percentile === minP && m.rank !== 'consistent';
  const isWorst = m => m.loss_percentile === maxP && m.rank !== 'consistent' && minP !== maxP;

  // Grey lines first so the highlighted ones paint on top.
  const greyFirst = maneuvers.slice().sort((a, b) => {
    const aHi = isBest(a) || isWorst(a) ? 1 : 0;
    const bHi = isBest(b) || isWorst(b) ? 1 : 0;
    return aHi - bHi;
  });

  greyFirst.forEach(m => {
    const series = m[channel.key];
    if (!series) return;
    const id = m.session_id + ':' + m.maneuver_id;
    const hovered = _ovState.hoverId === id;
    let color, alpha, width;
    if (hovered) {
      color = '#fff'; alpha = 1.0; width = 2.0;
    } else if (isBest(m)) {
      color = getComputedStyle(document.body).getPropertyValue('--success') || '#2a6';
      alpha = 0.8; width = 1.8;
    } else if (isWorst(m)) {
      color = getComputedStyle(document.body).getPropertyValue('--error') || '#c44';
      alpha = 0.8; width = 1.8;
    } else {
      color = getComputedStyle(document.body).getPropertyValue('--text-secondary') || '#888';
      alpha = 0.45; width = 1.0;
    }
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < axis.length; i++) {
      const v = series[i];
      if (v === null || v === undefined) {
        started = false; continue;
      }
      const x = xToPx(axis[i]);
      const y = yToPx(v);
      if (!started) { ctx.moveTo(x, y); started = true; }
      else { ctx.lineTo(x, y); }
    }
    ctx.stroke();
  });
  ctx.globalAlpha = 1.0;
}

function _ovRenderBands(ctx, panel, axis, maneuvers, channel, xToPx, yToPx) {
  // Compute p10/p25/p50/p75/p90 at each sample index across all maneuvers.
  const qs = { p10: [], p25: [], p50: [], p75: [], p90: [] };
  for (let i = 0; i < axis.length; i++) {
    const col = [];
    for (const m of maneuvers) {
      const v = m[channel.key] ? m[channel.key][i] : null;
      if (v !== null && v !== undefined) col.push(v);
    }
    if (col.length < 2) {
      qs.p10.push(null); qs.p25.push(null); qs.p50.push(null); qs.p75.push(null); qs.p90.push(null);
      continue;
    }
    col.sort((a, b) => a - b);
    qs.p10.push(_ovPercentile(col, 0.10));
    qs.p25.push(_ovPercentile(col, 0.25));
    qs.p50.push(_ovPercentile(col, 0.50));
    qs.p75.push(_ovPercentile(col, 0.75));
    qs.p90.push(_ovPercentile(col, 0.90));
  }

  // Fill p25→p75 envelope
  const accent = getComputedStyle(document.body).getPropertyValue('--accent') || '#4af';
  ctx.fillStyle = accent;
  ctx.globalAlpha = 0.25;
  ctx.beginPath();
  let started = false;
  for (let i = 0; i < axis.length; i++) {
    const v = qs.p25[i];
    if (v === null || v === undefined) continue;
    const x = xToPx(axis[i]);
    const y = yToPx(v);
    if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
  }
  for (let i = axis.length - 1; i >= 0; i--) {
    const v = qs.p75[i];
    if (v === null || v === undefined) continue;
    const x = xToPx(axis[i]);
    const y = yToPx(v);
    ctx.lineTo(x, y);
  }
  ctx.closePath();
  ctx.fill();
  ctx.globalAlpha = 1.0;

  // p10 and p90 dashed outer lines
  ctx.setLineDash([3, 3]);
  ctx.strokeStyle = accent;
  ctx.lineWidth = 1;
  [qs.p10, qs.p90].forEach(arr => _ovStrokeSeries(ctx, axis, arr, xToPx, yToPx));
  ctx.setLineDash([]);

  // p50 solid median
  ctx.strokeStyle = accent;
  ctx.lineWidth = 2;
  _ovStrokeSeries(ctx, axis, qs.p50, xToPx, yToPx);
}

function _ovPercentile(sortedCol, q) {
  if (!sortedCol.length) return null;
  const idx = q * (sortedCol.length - 1);
  const lo = Math.floor(idx), hi = Math.ceil(idx);
  if (lo === hi) return sortedCol[lo];
  return sortedCol[lo] + (sortedCol[hi] - sortedCol[lo]) * (idx - lo);
}

function _ovStrokeSeries(ctx, axis, series, xToPx, yToPx) {
  ctx.beginPath();
  let started = false;
  for (let i = 0; i < axis.length; i++) {
    const v = series[i];
    if (v === null || v === undefined) { started = false; continue; }
    const x = xToPx(axis[i]);
    const y = yToPx(v);
    if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
  }
  ctx.stroke();
}

// ---------------------------------------------------------------------------
// Hover
// ---------------------------------------------------------------------------

function _ovOnHover(evt, panel) {
  if (!_ovState.data) return;
  const rect = panel.canvas.getBoundingClientRect();
  const x = evt.clientX - rect.left;
  const y = evt.clientY - rect.top;
  const { padL, padT, plotW, plotH } = panel.bbox || {};
  if (padL === undefined) return;
  if (x < padL || x > padL + plotW || y < padT || y > padT + plotH) {
    _ovState.hoverId = null;
    _ovRender();
    return;
  }
  // Find the maneuver whose line is closest at this x.
  const t = OV_AXIS_MIN + ((x - padL) / plotW) * (OV_AXIS_MAX - OV_AXIS_MIN);
  const idx = Math.round(t - OV_AXIS_MIN);
  if (idx < 0 || idx >= _ovState.data.axis_s.length) return;
  const allValues = _ovState.data.maneuvers.map(m => m[panel.channel.key]?.[idx]).filter(v => v !== null && v !== undefined);
  if (!allValues.length) return;
  const yMin = Math.min(...allValues);
  const yMax = Math.max(...allValues);
  const range = yMax - yMin || 1;
  let best = null, bestDist = Infinity;
  _ovState.data.maneuvers.forEach(m => {
    const v = m[panel.channel.key]?.[idx];
    if (v === null || v === undefined) return;
    const cy = padT + (1 - (v - yMin) / range) * plotH;
    const d = Math.abs(cy - y);
    if (d < bestDist) { bestDist = d; best = m; }
  });
  if (best && bestDist < 20) {
    _ovState.hoverId = best.session_id + ':' + best.maneuver_id;
  } else {
    _ovState.hoverId = null;
  }
  _ovRender();
}

// ---------------------------------------------------------------------------
// Mode toggle
// ---------------------------------------------------------------------------

function ovSetMode(mode) {
  _ovState.mode = mode;
  ['ov-mode-lines', 'ov-mode-bands', 'ov-mode-auto'].forEach(id => {
    document.getElementById(id).classList.remove('active');
  });
  document.getElementById('ov-mode-' + mode).classList.add('active');
  _ovRender();
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', ovInit);
} else {
  ovInit();
}
