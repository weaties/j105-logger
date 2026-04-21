/* Shared maneuver visualisation helpers (#619 parity).
 *
 * Pulled out of session.js so that /maneuvers/overlay can render the
 * same wind-up track SVG without duplicating the renderer. Deliberately
 * decoupled from session-page globals — callers pass interactivity
 * hooks and tick-interval explicitly instead of the renderer reading
 * `_maneuverTickInterval` and friends.
 *
 * Public surface:
 *   mvRenderTrackSvg(tracks, opts) → SVG string
 *   mvTrackBounds(trackPointArrays) → bounds or null
 *   mvTableHtml(maneuvers, opts) → table HTML string
 *
 * Sign conventions match the existing session-page overlay (+y = upwind,
 * origin at entry). Colour palette follows CSS variables so it adapts
 * to the theme.
 */
'use strict';

function mvTrackBounds(trackPointArrays) {
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  trackPointArrays.forEach(tr => tr.forEach(p => {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }));
  if (!isFinite(minX)) return null;
  // Square-ish bounds with a bit of padding.
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const half = Math.max(10, Math.max(maxX - minX, maxY - minY) / 2 + 5);
  return { minX: cx - half, maxX: cx + half, minY: cy - half, maxY: cy + half };
}

/* tracks: array of objects, each
 *   { points, color, label, highlight?, traceKey?, ghost?, durationSec?,
 *     entryBsp?, dashArray? }
 *
 * opts:
 *   width, height, interactive, tickInterval,
 *   onTraceEnter(evt, traceKey), onTraceLeave(), onTraceClick(traceKey)
 */
function mvRenderTrackSvg(tracks, opts) {
  opts = opts || {};
  const w = opts.width || 260;
  const h = opts.height || 200;
  const pad = 12;
  const interactive = !!opts.interactive;
  const tickInterval = opts.tickInterval || 0;
  const onEnterName = opts.onTraceEnterName || '';
  const onLeaveName = opts.onTraceLeaveName || '';
  const onClickName = opts.onTraceClickName || '';
  const pointSets = tracks.map(t => t.points).filter(p => p && p.length);
  if (!pointSets.length) return '';

  const ghostYs = tracks.map(t => t.ghost).filter(v => v != null && !isNaN(v));
  const extras = ghostYs.map(y => ({ x: 0, y }));
  const b = mvTrackBounds(pointSets.concat([extras]));
  if (!b) return '';

  const sx = x => pad + (x - b.minX) / (b.maxX - b.minX) * (w - 2 * pad);
  const sy = y => (h - pad) - (y - b.minY) / (b.maxY - b.minY) * (h - 2 * pad);

  const scaleM = (b.maxX - b.minX);
  const gridStep = scaleM > 200 ? 50 : scaleM > 80 ? 20 : 10;
  const gridLines = [];
  for (let gx = Math.ceil(b.minX / gridStep) * gridStep; gx <= b.maxX; gx += gridStep) {
    gridLines.push('<line x1="' + sx(gx) + '" y1="' + pad + '" x2="' + sx(gx) + '" y2="' + (h - pad) + '" stroke="var(--border)" stroke-width="0.5"/>');
  }
  for (let gy = Math.ceil(b.minY / gridStep) * gridStep; gy <= b.maxY; gy += gridStep) {
    gridLines.push('<line x1="' + pad + '" y1="' + sy(gy) + '" x2="' + (w - pad) + '" y2="' + sy(gy) + '" stroke="var(--border)" stroke-width="0.5"/>');
  }

  const originX = sx(0), originY = sy(0);
  const crosshair = '<circle cx="' + originX + '" cy="' + originY + '" r="3" fill="var(--accent)"/>'
    + '<line x1="' + originX + '" y1="' + (originY - 8) + '" x2="' + originX + '" y2="' + (originY + 8) + '" stroke="var(--accent)" stroke-width="0.6"/>'
    + '<line x1="' + (originX - 8) + '" y1="' + originY + '" x2="' + (originX + 8) + '" y2="' + originY + '" stroke="var(--accent)" stroke-width="0.6"/>';

  const windLabels = '<text x="' + (w / 2) + '" y="10" text-anchor="middle" font-size="9" fill="var(--text-secondary)">↑ upwind</text>'
    + '<text x="' + (w / 2) + '" y="' + (h - 12) + '" text-anchor="middle" font-size="9" fill="var(--text-secondary)">↓ downwind</text>';

  // Actual-at-duration points for ghost reference projection.
  const actualAtDuration = tracks.map(t => {
    if (!t.points || !t.points.length || t.durationSec == null) return null;
    let best = null;
    let bestDt = Infinity;
    for (const p of t.points) {
      const dt = Math.abs(p.t - t.durationSec);
      if (dt < bestDt) { bestDt = dt; best = p; }
    }
    return best;
  });

  const ghostLines = tracks.map((t, i) => {
    if (t.ghost == null || isNaN(t.ghost)) return '';
    const gy1 = sy(0), gy2 = sy(t.ghost);
    let out = '<line x1="' + originX + '" y1="' + gy1 + '" x2="' + originX + '" y2="' + gy2
      + '" stroke="' + t.color + '" stroke-width="1" stroke-dasharray="3,3" opacity="0.7"/>'
      + '<circle cx="' + originX + '" cy="' + gy2 + '" r="2.5" fill="none" stroke="' + t.color
      + '" stroke-width="1.2" opacity="0.8"/>';

    const actual = actualAtDuration[i];
    if (actual) {
      const ax = sx(actual.x), ay = sy(actual.y);
      const projY = ay;
      out += '<circle cx="' + ax + '" cy="' + ay + '" r="3" fill="' + t.color
        + '" stroke="var(--bg-secondary)" stroke-width="1"/>';
      out += '<line x1="' + ax + '" y1="' + ay + '" x2="' + originX + '" y2="' + projY
        + '" stroke="' + t.color + '" stroke-width="1" stroke-dasharray="1,2" opacity="0.55"/>';
      out += '<line x1="' + originX + '" y1="' + projY + '" x2="' + originX + '" y2="' + gy2
        + '" stroke="' + t.color + '" stroke-width="2.5" opacity="0.9"/>';
      if (t.highlight) {
        const deltaM = t.ghost - actual.y;
        const midY = (projY + gy2) / 2;
        const label = (deltaM >= 0 ? '−' : '+') + Math.abs(deltaM).toFixed(1) + ' m';
        out += '<text x="' + (originX + 6) + '" y="' + (midY + 3) + '" font-size="10" fill="' + t.color
          + '" style="paint-order:stroke;stroke:var(--bg-secondary);stroke-width:3px;stroke-linejoin:round">'
          + label + '</text>';
      }
    }
    return out;
  }).join('');

  const decorations = tracks.map(t => {
    if (!t.points || !t.points.length) return '';
    let out = '';
    if (tickInterval > 0) {
      const ts = t.points.map(p => p.t).filter(v => v != null);
      if (ts.length) {
        const tMin = Math.min(...ts), tMax = Math.max(...ts);
        const kStart = Math.ceil(tMin / tickInterval);
        const kEnd = Math.floor(tMax / tickInterval);
        for (let k = kStart; k <= kEnd; k++) {
          const target = k * tickInterval;
          let best = null, bestDt = Infinity;
          for (const p of t.points) {
            const dt = Math.abs(p.t - target);
            if (dt < bestDt) { bestDt = dt; best = p; }
          }
          if (!best || bestDt > tickInterval / 2) continue;
          const cx = sx(best.x), cy = sy(best.y);
          out += '<circle cx="' + cx + '" cy="' + cy + '" r="1.8" fill="' + t.color
            + '" stroke="var(--bg-secondary)" stroke-width="0.5" opacity="0.9"/>';
          if (k !== 0 && t.highlight) {
            out += '<text x="' + (cx + 3) + '" y="' + (cy - 3) + '" font-size="8" fill="' + t.color
              + '" style="paint-order:stroke;stroke:var(--bg-secondary);stroke-width:2px;stroke-linejoin:round">'
              + target + 's</text>';
          }
        }
      }
    }
    if (t.entryBsp != null && t.durationSec != null) {
      const targets = [
        { frac: 0.8, shape: 'square', label: '80%' },
        { frac: 1.0, shape: 'diamond', label: '100%' },
      ];
      for (const tgt of targets) {
        const threshold = t.entryBsp * tgt.frac;
        let hit = null;
        for (const p of t.points) {
          if (p.t < t.durationSec) continue;
          if (p.bsp != null && p.bsp >= threshold) { hit = p; break; }
        }
        if (!hit) continue;
        const cx = sx(hit.x), cy = sy(hit.y);
        const r = 4;
        if (tgt.shape === 'square') {
          out += '<rect x="' + (cx - r) + '" y="' + (cy - r) + '" width="' + (2 * r)
            + '" height="' + (2 * r) + '" fill="none" stroke="' + t.color
            + '" stroke-width="1.4"/>';
        } else {
          out += '<polygon points="' + cx + ',' + (cy - r) + ' ' + (cx + r) + ',' + cy
            + ' ' + cx + ',' + (cy + r) + ' ' + (cx - r) + ',' + cy
            + '" fill="none" stroke="' + t.color + '" stroke-width="1.4"/>';
        }
        if (t.highlight) {
          out += '<text x="' + (cx + r + 2) + '" y="' + (cy + 3) + '" font-size="8" fill="' + t.color
            + '" style="paint-order:stroke;stroke:var(--bg-secondary);stroke-width:2px;stroke-linejoin:round">'
            + tgt.label + '</text>';
        }
      }
    }
    return out;
  }).join('');

  const _evtHandlers = (tk) => {
    if (!interactive || tk == null) return '';
    let h = '';
    if (onEnterName) h += ' onmouseenter="' + onEnterName + '(event,' + JSON.stringify(tk) + ')"';
    if (onLeaveName) h += ' onmouseleave="' + onLeaveName + '()"';
    if (onClickName) h += ' onclick="' + onClickName + '(' + JSON.stringify(tk) + ')"';
    return h;
  };

  const paths = tracks.map(t => {
    if (!t.points || !t.points.length) return '';
    const d = t.points.map((p, i) => (i === 0 ? 'M' : 'L') + sx(p.x).toFixed(1) + ' ' + sy(p.y).toFixed(1)).join(' ');
    const width = t.highlight ? 2.5 : 1.4;
    const opacity = t.highlight ? 1 : 0.7;
    let attrs = 'fill="none" stroke="' + t.color + '" stroke-width="' + width + '" opacity="' + opacity + '" stroke-linecap="round"';
    if (t.dashArray) attrs += ' stroke-dasharray="' + t.dashArray + '"';
    if (interactive && t.traceKey != null) {
      attrs += ' style="pointer-events:stroke;cursor:pointer"' + _evtHandlers(t.traceKey);
    }
    return '<path d="' + d + '" ' + attrs + '/>';
  }).join('');

  const hoverUnderlay = interactive ? tracks.map(t => {
    if (!t.points || !t.points.length || t.traceKey == null) return '';
    const d = t.points.map((p, i) => (i === 0 ? 'M' : 'L') + sx(p.x).toFixed(1) + ' ' + sy(p.y).toFixed(1)).join(' ');
    return '<path d="' + d + '" fill="none" stroke="rgba(0,0,0,0)" stroke-width="14"'
      + ' style="pointer-events:stroke;cursor:pointer"' + _evtHandlers(t.traceKey) + '/>';
  }).join('') : '';

  const scaleLabel = '<text x="' + (w - pad) + '" y="' + (h - 2) + '" text-anchor="end" font-size="9" fill="var(--text-secondary)">grid ' + gridStep + ' m</text>';

  return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:3px">'
    + gridLines.join('') + ghostLines + hoverUnderlay + paths + decorations + crosshair + windLabels + scaleLabel + '</svg>';
}

/* Shared maneuver-table HTML builder. Minimal by design — callers
 * layer checkboxes / filter state / selection state on top via CSS
 * classes and onclick handlers. Returns raw HTML ready for innerHTML. */
function mvTableHtml(maneuvers, opts) {
  opts = opts || {};
  const fmtTime = opts.fmtTime || (ts => (ts || '').slice(11, 19));
  const esc = opts.esc || (s => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const onRowClickName = opts.onRowClickName || '';
  const showSession = !!opts.showSession;

  const rows = maneuvers.map(m => {
    const key = m.session_id + ':' + m.maneuver_id;
    const typeCls = 'mv-badge-' + (m.type || '');
    const dirTxt = m.turn_angle_deg != null
      ? (m.turn_angle_deg < 0 ? 'P→S' : 'S→P') : '';
    const dur = m.duration_sec != null ? m.duration_sec.toFixed(1) + 's' : '—';
    const turn = m.turn_angle_deg != null ? Math.round(Math.abs(m.turn_angle_deg)) + '°' : '—';
    const bspIO = (m.entry_bsp != null && m.exit_bsp != null)
      ? m.entry_bsp.toFixed(1) + '→' + m.exit_bsp.toFixed(1) : '—';
    const bspDip = m.loss_kts != null ? m.loss_kts.toFixed(2) + ' kt' : '—';
    const distLoss = m.distance_loss_m != null ? Math.round(m.distance_loss_m) + ' m' : '—';
    const tws = m.entry_tws != null ? m.entry_tws.toFixed(1) + ' kt' : '—';
    const rank = m.rank || '';
    const pct = m.loss_percentile != null ? ' (p' + m.loss_percentile + ')' : '';
    const yt = m.youtube_url
      ? '<a href="' + esc(m.youtube_url) + '" target="_blank" rel="noopener" style="color:var(--accent);text-decoration:none">▶</a>'
      : '—';
    const rowAttrs = onRowClickName
      ? ' style="cursor:pointer" onclick="' + onRowClickName + '(\'' + key + '\')"'
      : '';
    const sessionCell = showSession
      ? '<td>' + esc((m.session_name || '').slice(0, 20)) + '</td>'
      : '';
    return '<tr data-trace-key="' + key + '"' + rowAttrs + '>'
      + sessionCell
      + '<td class="' + typeCls + '">' + esc(m.type || '') + '</td>'
      + '<td>' + esc(dirTxt) + '</td>'
      + '<td>' + esc(fmtTime(m.ts)) + '</td>'
      + '<td class="mv-num">' + dur + '</td>'
      + '<td class="mv-num">' + turn + '</td>'
      + '<td>' + bspIO + '</td>'
      + '<td>' + bspDip + '</td>'
      + '<td>' + distLoss + '</td>'
      + '<td>' + tws + '</td>'
      + '<td>' + esc(rank + pct) + '</td>'
      + '<td>' + yt + '</td>'
      + '</tr>';
  }).join('');

  const sessionHeader = showSession ? '<th>Session</th>' : '';
  return '<table class="mv-viz-table">'
    + '<thead><tr>' + sessionHeader
    + '<th>Type</th><th>Dir</th><th>Time</th>'
    + '<th class="mv-num">Dur</th><th class="mv-num">Turn</th>'
    + '<th>BSP in→out</th><th>BSP dip</th><th>Dist loss</th>'
    + '<th>TWS</th><th>Rank</th><th>Video</th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table>';
}

// ---------------------------------------------------------------------------
// Stacked line charts (BSP / heading rate / TWA), centred at head-to-wind.
//
// Builds three canvases inside `hostEl` and paints them from an overlay
// API payload ({axis_s, channels, maneuvers: [{bsp, heading_rate_deg_s,
// twa, loss_percentile, rank, ...}]}). Returns a controller with:
//   ctrl.setHoverId(id | null)
//   ctrl.setMode('lines' | 'bands' | 'auto')
//   ctrl.destroy()
// Callers can also pass `onHoverChange(id)` to propagate hover state to
// sibling views (e.g. the wind-up track SVG).
// ---------------------------------------------------------------------------

const MV_CHANNELS = [
  { key: 'bsp',                 label: 'Boat speed',    unit: 'kt',   decimals: 1 },
  { key: 'heading_rate_deg_s',  label: 'Heading rate',  unit: '°/s',  decimals: 1 },
  { key: 'twa',                 label: 'TWA',           unit: '°',    decimals: 0 },
];
const MV_BAND_AUTO_THRESHOLD = 15;
const MV_AXIS_MIN = -20;
const MV_AXIS_MAX = 30;

function _mvPercentile(sortedCol, q) {
  if (!sortedCol.length) return null;
  const idx = q * (sortedCol.length - 1);
  const lo = Math.floor(idx), hi = Math.ceil(idx);
  if (lo === hi) return sortedCol[lo];
  return sortedCol[lo] + (sortedCol[hi] - sortedCol[lo]) * (idx - lo);
}

function _mvStrokeSeries(ctx, axis, series, xToPx, yToPx) {
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

function mvInitLineCharts(hostEl, payload, opts) {
  opts = opts || {};
  const panelHeight = opts.panelHeight || 140;
  const panelPadClass = opts.panelClass || 'mv-chart-panel';
  const onHoverChange = opts.onHoverChange || null;

  const state = {
    data: payload,
    mode: opts.mode || 'auto',
    hoverId: null,
    panels: [],
  };

  hostEl.innerHTML = '';
  state.panels = MV_CHANNELS.map(ch => {
    const panel = document.createElement('div');
    panel.className = panelPadClass;
    panel.style.cssText = 'background:var(--bg-secondary);border:1px solid var(--border);'
      + 'border-radius:4px;margin-bottom:6px;padding:4px';
    panel.innerHTML =
      '<h4 style="font-size:.72rem;margin:0 0 2px 4px;color:var(--text-secondary);'
      + 'font-weight:500;letter-spacing:.04em;text-transform:uppercase">'
      + ch.label + ' (' + ch.unit + ')</h4>'
      + '<div style="position:relative;width:100%;height:' + panelHeight + 'px">'
      + '<canvas style="display:block;width:100%;height:100%"></canvas></div>';
    hostEl.appendChild(panel);
    const canvas = panel.querySelector('canvas');
    return { channel: ch, canvas, ctx: canvas.getContext('2d'), bbox: null };
  });

  function resolveMode() {
    if (state.mode !== 'auto') return state.mode;
    return (state.data.maneuvers.length >= MV_BAND_AUTO_THRESHOLD) ? 'bands' : 'lines';
  }

  function renderPanel(panel, mode) {
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

    const css = name => getComputedStyle(document.body).getPropertyValue(name).trim() || '';
    ctx.fillStyle = css('--bg-secondary') || '#1a1f26';
    ctx.fillRect(0, 0, cssW, cssH);

    const axis = state.data.axis_s;
    const maneuvers = state.data.maneuvers;
    const allValues = [];
    maneuvers.forEach(m => {
      const series = m[channel.key];
      if (!series) return;
      series.forEach(v => { if (v !== null && v !== undefined) allValues.push(v); });
    });
    if (!allValues.length) {
      ctx.fillStyle = css('--text-secondary') || '#888';
      ctx.font = '11px sans-serif';
      ctx.fillText('(no data in window)', padL + 6, padT + 12);
      return;
    }

    let yMin = Math.min(...allValues);
    let yMax = Math.max(...allValues);
    if (yMin === yMax) { yMin -= 1; yMax += 1; }
    const yPad = (yMax - yMin) * 0.08;
    yMin -= yPad; yMax += yPad;

    const xToPx = x => padL + ((x - MV_AXIS_MIN) / (MV_AXIS_MAX - MV_AXIS_MIN)) * plotW;
    const yToPx = y => padT + (1 - (y - yMin) / (yMax - yMin)) * plotH;

    // Grid: vertical 0-line (HTW marker).
    ctx.strokeStyle = 'rgba(180,180,180,0.25)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(xToPx(0), padT);
    ctx.lineTo(xToPx(0), padT + plotH);
    ctx.stroke();

    ctx.fillStyle = css('--text-secondary') || '#888';
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

    if (mode === 'bands') renderBands(ctx, axis, maneuvers, channel, xToPx, yToPx, css);
    else renderLines(ctx, axis, maneuvers, channel, xToPx, yToPx, css);

    ctx.strokeStyle = css('--border') || '#333';
    ctx.strokeRect(padL, padT, plotW, plotH);
  }

  function renderLines(ctx, axis, maneuvers, channel, xToPx, yToPx, css) {
    if (!maneuvers.length) return;
    const pctls = maneuvers.map(m => m.loss_percentile).filter(v => v !== null && v !== undefined);
    const minP = pctls.length ? Math.min(...pctls) : null;
    const maxP = pctls.length ? Math.max(...pctls) : null;
    const isBest = m => m.loss_percentile === minP && m.rank !== 'consistent';
    const isWorst = m => m.loss_percentile === maxP && m.rank !== 'consistent' && minP !== maxP;

    const greyFirst = maneuvers.slice().sort((a, b) => {
      const aHi = isBest(a) || isWorst(a) ? 1 : 0;
      const bHi = isBest(b) || isWorst(b) ? 1 : 0;
      return aHi - bHi;
    });

    greyFirst.forEach(m => {
      const series = m[channel.key];
      if (!series) return;
      const id = m.session_id + ':' + m.maneuver_id;
      const hovered = state.hoverId === id;
      let color, alpha, width;
      if (hovered) { color = '#fff'; alpha = 1.0; width = 2.0; }
      else if (isBest(m)) { color = css('--success') || '#2a6'; alpha = 0.8; width = 1.8; }
      else if (isWorst(m)) { color = css('--error') || '#c44'; alpha = 0.8; width = 1.8; }
      else { color = css('--text-secondary') || '#888'; alpha = 0.45; width = 1.0; }
      ctx.globalAlpha = alpha;
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      _mvStrokeSeries(ctx, axis, series, xToPx, yToPx);
    });
    ctx.globalAlpha = 1.0;
  }

  function renderBands(ctx, axis, maneuvers, channel, xToPx, yToPx, css) {
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
      qs.p10.push(_mvPercentile(col, 0.10));
      qs.p25.push(_mvPercentile(col, 0.25));
      qs.p50.push(_mvPercentile(col, 0.50));
      qs.p75.push(_mvPercentile(col, 0.75));
      qs.p90.push(_mvPercentile(col, 0.90));
    }
    const accent = css('--accent') || '#4af';
    ctx.fillStyle = accent;
    ctx.globalAlpha = 0.25;
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < axis.length; i++) {
      const v = qs.p25[i]; if (v === null || v === undefined) continue;
      const x = xToPx(axis[i]), y = yToPx(v);
      if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
    }
    for (let i = axis.length - 1; i >= 0; i--) {
      const v = qs.p75[i]; if (v === null || v === undefined) continue;
      ctx.lineTo(xToPx(axis[i]), yToPx(v));
    }
    ctx.closePath();
    ctx.fill();
    ctx.globalAlpha = 1.0;

    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1;
    [qs.p10, qs.p90].forEach(arr => _mvStrokeSeries(ctx, axis, arr, xToPx, yToPx));
    ctx.setLineDash([]);
    ctx.strokeStyle = accent;
    ctx.lineWidth = 2;
    _mvStrokeSeries(ctx, axis, qs.p50, xToPx, yToPx);
  }

  function render() {
    if (!state.data) return;
    const mode = resolveMode();
    state.panels.forEach(p => renderPanel(p, mode));
  }

  function onHover(evt, panel) {
    const rect = panel.canvas.getBoundingClientRect();
    const x = evt.clientX - rect.left;
    const y = evt.clientY - rect.top;
    const bb = panel.bbox;
    if (!bb || x < bb.padL || x > bb.padL + bb.plotW ||
        y < bb.padT || y > bb.padT + bb.plotH) {
      setHoverId(null);
      return;
    }
    const t = MV_AXIS_MIN + ((x - bb.padL) / bb.plotW) * (MV_AXIS_MAX - MV_AXIS_MIN);
    const idx = Math.round(t - MV_AXIS_MIN);
    if (idx < 0 || idx >= state.data.axis_s.length) return;
    const vals = state.data.maneuvers.map(m => m[panel.channel.key]?.[idx])
      .filter(v => v !== null && v !== undefined);
    if (!vals.length) return;
    const yMin = Math.min(...vals), yMax = Math.max(...vals);
    const range = yMax - yMin || 1;
    let best = null, bestDist = Infinity;
    state.data.maneuvers.forEach(m => {
      const v = m[panel.channel.key]?.[idx];
      if (v === null || v === undefined) return;
      const cy = bb.padT + (1 - (v - yMin) / range) * bb.plotH;
      const d = Math.abs(cy - y);
      if (d < bestDist) { bestDist = d; best = m; }
    });
    setHoverId((best && bestDist < 20) ? (best.session_id + ':' + best.maneuver_id) : null);
  }

  function setHoverId(id) {
    if (state.hoverId === id) return;
    state.hoverId = id;
    render();
    if (onHoverChange) onHoverChange(id);
  }

  function setMode(mode) {
    state.mode = mode;
    render();
  }

  state.panels.forEach(p => {
    p.canvas.addEventListener('mousemove', evt => onHover(evt, p));
    p.canvas.addEventListener('mouseleave', () => setHoverId(null));
  });

  const onResize = () => render();
  window.addEventListener('resize', onResize);

  // First paint needs a tick so the canvas has a clientWidth.
  setTimeout(render, 0);

  return {
    setHoverId,
    setMode,
    render,
    destroy() {
      window.removeEventListener('resize', onResize);
      hostEl.innerHTML = '';
    },
  };
}

// Expose for same-window module-less usage.
window.mvRenderTrackSvg = mvRenderTrackSvg;
window.mvTrackBounds = mvTrackBounds;
window.mvTableHtml = mvTableHtml;
window.mvInitLineCharts = mvInitLineCharts;
