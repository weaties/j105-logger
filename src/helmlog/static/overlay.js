/* Multi-maneuver time-aligned overlay chart (#619).
 *
 * One page, one endpoint, two entry points — launched from a session
 * detail page with every tack/gybe pre-selected, or from the
 * cross-session maneuvers browser with the current checkbox selection.
 *
 * Layout:
 *   1. Wind-up track SVG (shared with the session-page overlay).
 *   2. Stacked line charts (BSP / heading rate / TWA) centred at HTW.
 *   3. Maneuver table.
 *
 * All three views share a single hover state: hovering a track, a line,
 * or a row highlights the others.
 *
 * Rendering helpers live in maneuver_viz.js so the session page can
 * reuse the same charts inline.
 */
'use strict';

const _ovState = {
  data: null,
  hoverId: null,
  chartsCtrl: null,
};

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

  if (!ids) { emptyEl.style.display = ''; return; }

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

  legendEl.style.display = '';

  // 1. Wind-up track SVG (top).
  _ovRenderTrackOverlay();

  // 2. Stacked line charts (middle) — shared with session-page inline view.
  _ovState.chartsCtrl = window.mvInitLineCharts(panelsEl, payload, {
    mode: 'auto',
    panelHeight: 160,
    onHoverChange: id => { _ovState.hoverId = id; _ovSyncTableHighlight(); _ovSyncTrackHighlight(); },
  });

  // 3. Maneuver table (bottom).
  _ovRenderTable();
}

// ---------------------------------------------------------------------------
// Wind-up track SVG — uses shared mvRenderTrackSvg.
// ---------------------------------------------------------------------------

function _ovRankColor(m) {
  const css = name => getComputedStyle(document.body).getPropertyValue(name).trim() || '#888';
  if (!m.rank || m.rank === 'consistent' || m.rank === 'avg') return css('--text-secondary');
  if (m.rank === 'good') return css('--success');
  if (m.rank === 'bad') return css('--error');
  return css('--text-secondary');
}

function _ovRenderTrackOverlay() {
  const wrap = document.getElementById('ov-track-wrap');
  const host = document.getElementById('ov-track-svg');
  if (!wrap || !host || !_ovState.data) return;
  const withTracks = _ovState.data.maneuvers.filter(m => m.track && m.track.length);
  if (!withTracks.length) { wrap.style.display = 'none'; return; }
  const tracks = withTracks.map(m => ({
    points: m.track,
    color: _ovRankColor(m),
    label: m.type,
    highlight: _ovState.hoverId === (m.session_id + ':' + m.maneuver_id),
    traceKey: m.session_id + ':' + m.maneuver_id,
    ghost: m.ghost_m,
    durationSec: m.duration_sec,
    entryBsp: m.entry_bsp,
  }));
  host.innerHTML = window.mvRenderTrackSvg(tracks, {
    width: 440,
    height: 380,
    interactive: true,
    tickInterval: 0,
    onTraceEnterName: 'ovOnTraceEnter',
    onTraceLeaveName: 'ovOnTraceLeave',
    onTraceClickName: 'ovOnTraceClick',
  });
  wrap.style.display = '';
}

function _ovSyncTrackHighlight() {
  // Re-render the SVG so the ``highlight`` flag on tracks updates.
  _ovRenderTrackOverlay();
}

window.ovOnTraceEnter = function (evt, traceKey) {
  _ovState.hoverId = traceKey;
  if (_ovState.chartsCtrl) _ovState.chartsCtrl.setHoverId(traceKey);
  _ovSyncTableHighlight();
  _ovSyncTrackHighlight();
};
window.ovOnTraceLeave = function () {
  _ovState.hoverId = null;
  if (_ovState.chartsCtrl) _ovState.chartsCtrl.setHoverId(null);
  _ovSyncTableHighlight();
  _ovSyncTrackHighlight();
};
window.ovOnTraceClick = function (traceKey) {
  _ovState.hoverId = traceKey;
  if (_ovState.chartsCtrl) _ovState.chartsCtrl.setHoverId(traceKey);
  _ovSyncTableHighlight();
  _ovSyncTrackHighlight();
};

// ---------------------------------------------------------------------------
// Maneuver table — uses shared mvTableHtml.
// ---------------------------------------------------------------------------

function _ovRenderTable() {
  const wrap = document.getElementById('ov-table-wrap');
  const host = document.getElementById('ov-table');
  if (!wrap || !host || !_ovState.data) return;
  const maneuvers = _ovState.data.maneuvers;
  if (!maneuvers.length) { wrap.style.display = 'none'; return; }
  const uniqueSessions = new Set(maneuvers.map(m => m.session_id));
  host.innerHTML = window.mvTableHtml(maneuvers, {
    showSession: uniqueSessions.size > 1,
    onRowClickName: 'ovOnTableRowClick',
  });
  wrap.style.display = '';
  _ovSyncTableHighlight();
}

function _ovSyncTableHighlight() {
  const host = document.getElementById('ov-table');
  if (!host) return;
  host.querySelectorAll('tr[data-trace-key]').forEach(tr => {
    if (tr.getAttribute('data-trace-key') === _ovState.hoverId) tr.classList.add('highlight');
    else tr.classList.remove('highlight');
  });
}

window.ovOnTableRowClick = function (traceKey) {
  const newId = _ovState.hoverId === traceKey ? null : traceKey;
  _ovState.hoverId = newId;
  if (_ovState.chartsCtrl) _ovState.chartsCtrl.setHoverId(newId);
  _ovSyncTableHighlight();
  _ovSyncTrackHighlight();
};

// ---------------------------------------------------------------------------
// Mode toggle
// ---------------------------------------------------------------------------

function ovSetMode(mode) {
  if (_ovState.chartsCtrl) _ovState.chartsCtrl.setMode(mode);
  ['ov-mode-lines', 'ov-mode-bands', 'ov-mode-auto'].forEach(id => {
    document.getElementById(id).classList.remove('active');
  });
  document.getElementById('ov-mode-' + mode).classList.add('active');
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', ovInit);
} else {
  ovInit();
}
