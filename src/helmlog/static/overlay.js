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
  data: null,      // full API payload — never mutated
  hoverId: null,
  chartsCtrl: null,
  // Filter and selection are client-side concerns. The endpoint's
  // `ids=` URL param sets the outer bound; these narrow what's
  // *displayed* (filter) and what's *acted on* (selection).
  filter: new Set(),   // active pill keys: 'tack', 'P→S', 'bad', 'tws:8-12', ...
  selected: new Set(), // "sid:mid" keys that are checked
};

const _OV_TYPE_PILLS = ['tack', 'gybe', 'rounding'];
const _OV_DIR_PILLS = ['P→S', 'S→P'];
const _OV_RANK_PILLS = ['good', 'bad'];
const _OV_TWS_PILLS = ['tws:0-8', 'tws:8-12', 'tws:12+'];

function _ovMatchesFilter(m) {
  if (!_ovState.filter.size) return true;
  const activeTypes = _OV_TYPE_PILLS.filter(p => _ovState.filter.has(p));
  if (activeTypes.length && !activeTypes.includes(m.type)) return false;
  const activeRanks = _OV_RANK_PILLS.filter(p => _ovState.filter.has(p));
  if (activeRanks.length && !activeRanks.includes(m.rank)) return false;
  const activeDir = _OV_DIR_PILLS.filter(p => _ovState.filter.has(p));
  if (activeDir.length) {
    if (m.turn_angle_deg == null) return false;
    const isPS = m.turn_angle_deg < 0;  // P→S = negative (matches session page)
    if (activeDir.includes('P→S') && !isPS) return false;
    if (activeDir.includes('S→P') && isPS) return false;
  }
  const activeTws = _OV_TWS_PILLS.filter(p => _ovState.filter.has(p));
  if (activeTws.length) {
    if (m.entry_tws == null) return false;
    const tws = m.entry_tws;
    const hit = activeTws.some(p => {
      if (p === 'tws:0-8') return tws < 8;
      if (p === 'tws:8-12') return tws >= 8 && tws < 12;
      if (p === 'tws:12+') return tws >= 12;
      return false;
    });
    if (!hit) return false;
  }
  return true;
}

function _ovFilteredManeuvers() {
  if (!_ovState.data) return [];
  return _ovState.data.maneuvers.filter(_ovMatchesFilter);
}

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
  document.getElementById('ov-select-bar').style.display = '';

  // Default: every loaded maneuver is selected so Compare Selected
  // works out of the box. User can uncheck individually.
  payload.maneuvers.forEach(m => {
    _ovState.selected.add(m.session_id + ':' + m.maneuver_id);
  });

  _ovRenderAll();
}

function _ovRenderAll() {
  // Filter narrows the visible set; every panel paints from the
  // filtered subset so the user sees the same maneuvers across all
  // three views.
  const visible = _ovFilteredManeuvers();
  const visiblePayload = Object.assign({}, _ovState.data, { maneuvers: visible });

  // 1. Wind-up track SVG (top).
  _ovRenderTrackOverlay(visible);

  // 2. Stacked line charts (middle).
  if (_ovState.chartsCtrl) _ovState.chartsCtrl.destroy();
  _ovState.chartsCtrl = window.mvInitLineCharts(
    document.getElementById('ov-panels'),
    visiblePayload,
    {
      mode: 'auto',
      panelHeight: 160,
      onHoverChange: id => {
        _ovState.hoverId = id;
        _ovSyncTableHighlight();
        _ovSyncTrackHighlight();
      },
    }
  );

  // 3. Maneuver table (bottom).
  _ovRenderTable(visible);

  // Select-bar counter + Compare button state.
  _ovUpdateSelectBar();
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

function _ovRenderTrackOverlay(visible) {
  const wrap = document.getElementById('ov-track-wrap');
  const host = document.getElementById('ov-track-svg');
  if (!wrap || !host || !_ovState.data) return;
  const src = visible || _ovState.data.maneuvers;
  const withTracks = src.filter(m => m.track && m.track.length);
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
  _ovRenderTrackOverlay(_ovFilteredManeuvers());
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

function _ovRenderTable(visible) {
  const wrap = document.getElementById('ov-table-wrap');
  const host = document.getElementById('ov-table');
  if (!wrap || !host || !_ovState.data) return;
  const maneuvers = visible || _ovState.data.maneuvers;
  if (!maneuvers.length) { wrap.style.display = 'none'; return; }
  const uniqueSessions = new Set(maneuvers.map(m => m.session_id));
  host.innerHTML = window.mvTableHtml(maneuvers, {
    showSession: uniqueSessions.size > 1,
    onRowClickName: 'ovOnTableRowClick',
    checked: _ovState.selected,
    onCheckName: 'ovOnRowCheck',
    onCheckAllName: 'ovOnCheckAll',
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
// Filter pills
// ---------------------------------------------------------------------------

function ovToggleFilter() {
  const panel = document.getElementById('ov-filter-panel');
  const btn = document.getElementById('ov-filter-toggle');
  const open = panel.classList.toggle('open');
  if (btn) btn.classList.toggle('active', open);
}

function ovTogglePill(key) {
  if (_ovState.filter.has(key)) _ovState.filter.delete(key);
  else _ovState.filter.add(key);
  _ovPaintPills();
  _ovRenderAll();
}

function ovClearFilters() {
  _ovState.filter.clear();
  _ovPaintPills();
  _ovRenderAll();
}

function _ovPaintPills() {
  document.querySelectorAll('.ov-pill[data-filter]').forEach(el => {
    const k = el.getAttribute('data-filter');
    el.classList.toggle('active', _ovState.filter.has(k));
  });
}

// ---------------------------------------------------------------------------
// Selection + Compare
// ---------------------------------------------------------------------------

window.ovOnRowCheck = function (traceKey, isChecked) {
  if (isChecked) _ovState.selected.add(traceKey);
  else _ovState.selected.delete(traceKey);
  _ovUpdateSelectBar();
};

window.ovOnCheckAll = function (isChecked) {
  // Applies to the currently visible (filtered) set only — matches the
  // user's mental model when filters are active.
  const visible = _ovFilteredManeuvers();
  if (isChecked) {
    visible.forEach(m => _ovState.selected.add(m.session_id + ':' + m.maneuver_id));
  } else {
    visible.forEach(m => _ovState.selected.delete(m.session_id + ':' + m.maneuver_id));
  }
  _ovRenderTable(visible);
  _ovUpdateSelectBar();
};

function ovSelectAll(mode) {
  if (!_ovState.data) return;
  if (mode === 'none') {
    _ovState.selected.clear();
  } else if (mode === 'all') {
    _ovState.data.maneuvers.forEach(m => {
      _ovState.selected.add(m.session_id + ':' + m.maneuver_id);
    });
  } else if (mode === 'filtered') {
    // Narrow selection to currently-visible rows.
    _ovState.selected.clear();
    _ovFilteredManeuvers().forEach(m => {
      _ovState.selected.add(m.session_id + ':' + m.maneuver_id);
    });
  }
  _ovRenderTable(_ovFilteredManeuvers());
  _ovUpdateSelectBar();
}

function _ovUpdateSelectBar() {
  const n = _ovState.selected.size;
  const countEl = document.getElementById('ov-select-count');
  if (countEl) countEl.textContent = n + ' selected';
  const btn = document.getElementById('ov-compare-btn');
  if (!btn || !_ovState.data) return;
  // Compare needs at least one selected maneuver that has a YouTube
  // link, otherwise there's nothing to play.
  const withVideo = _ovState.data.maneuvers.filter(
    m => _ovState.selected.has(m.session_id + ':' + m.maneuver_id) && m.youtube_url
  );
  btn.disabled = withVideo.length === 0;
}

function ovCompareSelected() {
  if (!_ovState.data) return;
  // Preserve the table order so cells appear in the same sequence the
  // user saw while selecting. Filter doesn't matter here — selection
  // carries across filter changes.
  const ordered = _ovState.data.maneuvers
    .map(m => m.session_id + ':' + m.maneuver_id)
    .filter(k => _ovState.selected.has(k));
  if (!ordered.length) return;
  window.open('/compare?ids=' + ordered.join(','), '_blank');
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', ovInit);
} else {
  ovInit();
}
