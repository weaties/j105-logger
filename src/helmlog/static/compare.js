/* compare.js — Synced multi-video maneuver comparison (#565)
 *
 * Loads maneuver data from the compare API, creates one YouTube IFrame
 * player per maneuver, and wires them to a common play/pause control.
 * Each player is cued to [maneuver_ts - preroll + globalNudge + perVideoNudge].
 * The grid auto-sizes so all videos fit in the viewport without scrolling.
 */

'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const SESSION_ID = document.getElementById('app-config').dataset.sessionId;
let _players = [];   // { player, cueSeconds, maneuver, idx, nudge }
let _allManeuvers = [];
let _videoSync = null;
let _playing = false;
let _prerollS = 10;
let _globalNudge = 0;  // seconds, applied to all videos (#568)
let _ytReady = false;

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

(async function init() {
  _loadYouTubeAPI();
  const ids = new URLSearchParams(window.location.search).get('ids');
  if (!ids) { _showEmpty(); return; }
  const resp = await fetch(`/api/sessions/${SESSION_ID}/maneuvers/compare?ids=${ids}`);
  if (!resp.ok) { _showEmpty(); return; }
  const data = await resp.json();
  const maneuvers = data.maneuvers || [];
  _videoSync = data.video_sync;
  if (!maneuvers.length || !_videoSync) { _showEmpty(); return; }

  _allManeuvers = maneuvers.filter(m => m.youtube_url);
  if (!_allManeuvers.length) { _showEmpty(); return; }

  _buildGrid();
  document.getElementById('compare-controls').style.display = '';
  window.addEventListener('resize', _onResize);
})();

// ---------------------------------------------------------------------------
// YouTube IFrame API
// ---------------------------------------------------------------------------

function _loadYouTubeAPI() {
  if (window.YT && window.YT.Player) { _ytReady = true; return; }
  const tag = document.createElement('script');
  tag.src = 'https://www.youtube.com/iframe_api';
  document.head.appendChild(tag);
}

window.onYouTubeIframeAPIReady = function () {
  _ytReady = true;
  _createPendingPlayers();
};

let _pendingPlayers = [];

function _createPendingPlayers() {
  for (const p of _pendingPlayers) {
    _createPlayer(p.divId, p.videoId, p.cueSeconds, p.maneuver, p.idx, p.nudge);
  }
  _pendingPlayers = [];
}

// ---------------------------------------------------------------------------
// Cue point calculation
// ---------------------------------------------------------------------------

function _calcCue(maneuver, nudge) {
  return Math.max(0, (maneuver.video_offset_s || 0) - _prerollS + _globalNudge + (nudge || 0));
}

// ---------------------------------------------------------------------------
// Grid layout — fit all cells in the viewport
// ---------------------------------------------------------------------------

function _gridLayout(n) {
  if (n <= 1) return { cols: 1, rows: 1 };
  if (n <= 2) return { cols: 2, rows: 1 };
  if (n <= 4) return { cols: 2, rows: 2 };
  if (n <= 6) return { cols: 3, rows: 2 };
  if (n <= 9) return { cols: 3, rows: 3 };
  if (n <= 12) return { cols: 4, rows: 3 };
  if (n <= 16) return { cols: 4, rows: 4 };
  const cols = Math.ceil(Math.sqrt(n * 16 / 9));
  return { cols, rows: Math.ceil(n / cols) };
}

function _buildGrid() {
  const grid = document.getElementById('compare-grid');
  // Preserve per-video nudges across rebuilds
  const nudges = {};
  _players.forEach(p => { nudges[p.maneuver.id] = p.nudge || 0; });

  grid.innerHTML = '';
  _players = [];
  _pendingPlayers = [];

  const n = _allManeuvers.length;
  if (!n) { _showEmpty(); return; }

  const layout = _gridLayout(n);
  const headerEl = document.getElementById('compare-header');
  const headerH = headerEl ? headerEl.offsetHeight + 4 : 40;
  const availH = window.innerHeight - headerH - 12;
  const availW = window.innerWidth - 12;
  const gap = 4;
  const labelH = 20;
  const cellPad = 2;

  const cellH = (availH - gap * (layout.rows - 1)) / layout.rows;
  const videoH = cellH - labelH - cellPad;

  grid.style.gridTemplateColumns = 'repeat(' + layout.cols + ', 1fr)';
  grid.style.height = availH + 'px';

  _updateSubtitle();

  for (let i = 0; i < _allManeuvers.length; i++) {
    const m = _allManeuvers[i];
    const nudge = nudges[m.id] || 0;
    const cueSeconds = _calcCue(m, nudge);
    const divId = 'yt-compare-' + i;

    const cell = document.createElement('div');
    cell.className = 'compare-cell';
    cell.id = 'compare-cell-' + i;
    cell.style.maxHeight = cellH + 'px';

    const typeClass = 'badge-' + (m.type || 'maneuver');
    const dirHint = _directionHint(m);
    const dur = m.duration_sec != null ? m.duration_sec.toFixed(1) + 's' : '';
    const loss = m.loss_kts != null ? m.loss_kts.toFixed(2) + ' kt dip' : '';
    const rank = m.rank ? (' <span style="color:' + _rankColor(m.rank) + '">&#9679; ' + m.rank + '</span>') : '';
    const elapsed = _fmtElapsed(m.ts);
    const bsp = (m.entry_bsp != null ? m.entry_bsp.toFixed(1) : '?') + '&#8594;' + (m.exit_bsp != null ? m.exit_bsp.toFixed(1) : '?');
    const turn = m.turn_angle_deg != null ? Math.abs(Math.round(m.turn_angle_deg)) + '&deg;' : '';

    const nudgeDisplay = nudge !== 0 ? (nudge > 0 ? '+' : '') + nudge.toFixed(1) + 's' : '0.0s';

    cell.innerHTML =
      '<button class="cell-dismiss" onclick="dismissCell(' + i + ')" title="Remove from comparison">&#10005;</button>'
      + '<div class="yt-wrap" id="' + divId + '" style="height:' + Math.max(60, videoH) + 'px"></div>'
      + '<div class="cell-label">'
      + '<b class="' + typeClass + '">' + _esc(m.type || 'maneuver') + '</b>'
      + dirHint + rank + ' ' + elapsed
      + (turn ? ' &middot; ' + turn : '')
      + ' &middot; ' + bsp + ' kt'
      + (dur ? ' &middot; ' + dur : '')
      + (loss ? ' &middot; ' + loss : '')
      + '<span class="nudge-controls">'
      + '<button onclick="event.stopPropagation();nudgeVideo(' + i + ',-0.5)" title="-0.5s">&#9664;</button>'
      + '<span class="nudge-value" id="nudge-val-' + i + '">' + nudgeDisplay + '</span>'
      + '<button onclick="event.stopPropagation();nudgeVideo(' + i + ',+0.5)" title="+0.5s">&#9654;</button>'
      + '<button onclick="event.stopPropagation();nudgeVideo(' + i + ',0,true)" title="Reset offset" class="nudge-reset">0</button>'
      + '</span>'
      + '</div>';
    grid.appendChild(cell);

    const entry = { divId, videoId: _videoSync.video_id, cueSeconds, maneuver: m, idx: i, nudge };
    if (_ytReady) {
      _createPlayer(divId, _videoSync.video_id, cueSeconds, m, i, nudge);
    } else {
      _pendingPlayers.push(entry);
    }
  }
}

let _resizeTimer = 0;
function _onResize() {
  clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(_buildGrid, 200);
}

function _updateSubtitle() {
  document.getElementById('compare-subtitle').textContent =
    _allManeuvers.length + ' maneuver' + (_allManeuvers.length !== 1 ? 's' : '');
}

// ---------------------------------------------------------------------------
// Player creation
// ---------------------------------------------------------------------------

function _createPlayer(divId, videoId, cueSeconds, maneuver, idx, nudge) {
  const player = new YT.Player(divId, {
    width: '100%',
    height: '100%',
    videoId: videoId,
    playerVars: {
      start: Math.max(0, Math.floor(cueSeconds)),
      controls: 1,
      modestbranding: 1,
      rel: 0,
    },
    events: {
      onReady: function (ev) {
        ev.target.seekTo(cueSeconds, true);
        ev.target.pauseVideo();
        const speed = parseFloat(document.getElementById('speed-select').value) || 1;
        ev.target.setPlaybackRate(speed);
      },
    },
  });
  _players.push({ player, cueSeconds, maneuver, idx, nudge: nudge || 0 });
}

// ---------------------------------------------------------------------------
// Per-video nudge (#567)
// ---------------------------------------------------------------------------

function nudgeVideo(idx, delta, reset) {
  const p = _players.find(p => p.idx === idx);
  if (!p) return;
  if (reset) {
    p.nudge = 0;
  } else {
    p.nudge = Math.round((p.nudge + delta) * 10) / 10;
  }
  p.cueSeconds = _calcCue(p.maneuver, p.nudge);
  try {
    p.player.seekTo(p.cueSeconds, true);
    if (!_playing) p.player.pauseVideo();
  } catch (_e) { /* not ready */ }
  // Update the display
  const el = document.getElementById('nudge-val-' + idx);
  if (el) {
    const v = p.nudge;
    el.textContent = (v > 0 ? '+' : '') + v.toFixed(1) + 's';
  }
}

// ---------------------------------------------------------------------------
// Dismiss (remove a cell)
// ---------------------------------------------------------------------------

function dismissCell(idx) {
  const pi = _players.findIndex(p => p.idx === idx);
  if (pi !== -1) {
    try { _players[pi].player.destroy(); } catch (_e) { /* ok */ }
    _players.splice(pi, 1);
  }
  _allManeuvers.splice(idx, 1);
  if (!_allManeuvers.length) {
    _showEmpty();
    document.getElementById('compare-grid').style.display = 'none';
    return;
  }
  _buildGrid();
}

// ---------------------------------------------------------------------------
// Shared controls
// ---------------------------------------------------------------------------

function togglePlayAll() {
  if (_playing) {
    _playing = false;
    _players.forEach(p => { try { p.player.pauseVideo(); } catch (_e) { /* not ready */ } });
    document.getElementById('play-all-btn').innerHTML = '&#9654; Play All';
  } else {
    _playing = true;
    _players.forEach(p => { try { p.player.playVideo(); } catch (_e) { /* not ready */ } });
    document.getElementById('play-all-btn').innerHTML = '&#9646;&#9646; Pause All';
  }
}

function seekAllToStart() {
  _playing = false;
  document.getElementById('play-all-btn').innerHTML = '&#9654; Play All';
  _players.forEach(p => {
    p.cueSeconds = _calcCue(p.maneuver, p.nudge);
    try {
      p.player.seekTo(p.cueSeconds, true);
      p.player.pauseVideo();
    } catch (_e) { /* not ready */ }
  });
}

function setAllSpeed(val) {
  const rate = parseFloat(val) || 1;
  _players.forEach(p => { try { p.player.setPlaybackRate(rate); } catch (_e) { /* not ready */ } });
}

function setPreroll(val) {
  _prerollS = parseInt(val, 10) || 10;
  _seekAllToCue();
}

// Global offset (#568)
function adjustGlobalOffset(delta) {
  _globalNudge = Math.round((_globalNudge + delta) * 10) / 10;
  document.getElementById('global-offset-val').textContent =
    (_globalNudge > 0 ? '+' : '') + _globalNudge.toFixed(1) + 's';
  _seekAllToCue();
}

function resetGlobalOffset() {
  _globalNudge = 0;
  document.getElementById('global-offset-val').textContent = '0.0s';
  _seekAllToCue();
}

function _seekAllToCue() {
  _playing = false;
  document.getElementById('play-all-btn').innerHTML = '&#9654; Play All';
  _players.forEach(p => {
    p.cueSeconds = _calcCue(p.maneuver, p.nudge);
    try {
      p.player.seekTo(p.cueSeconds, true);
      p.player.pauseVideo();
    } catch (_e) { /* not ready */ }
  });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _showEmpty() {
  document.getElementById('compare-grid').style.display = 'none';
  document.getElementById('compare-empty').style.display = '';
}

function _directionHint(m) {
  if ((m.type === 'tack' || m.type === 'gybe') && m.turn_angle_deg != null) {
    return m.turn_angle_deg > 0
      ? ' <span style="color:var(--text-secondary);font-size:.68rem">S&#8594;P</span>'
      : ' <span style="color:var(--text-secondary);font-size:.68rem">P&#8594;S</span>';
  }
  return '';
}

function _rankColor(rank) {
  const m = { good: 'var(--success)', bad: 'var(--error)', avg: 'var(--text-secondary)' };
  return m[rank] || 'var(--text-secondary)';
}

function _fmtElapsed(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch (_e) { return ''; }
}

function _esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
