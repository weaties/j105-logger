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
let _trackOverlayVisible = true;
let _tickInterval = 0; // playback position poll timer
let _sessionTrack = null; // { coords: [[lng,lat],...], timestamps: [iso,...] }
let _replaySamples = null; // [{ts:Date, hdg, cog, stw, sog, tws, twa, twd, aws, awa}]
let _gaugeVisible = true;

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

  // Fetch session track and replay data in parallel
  const [trackResult, replayResult] = await Promise.allSettled([
    fetch(`/api/sessions/${SESSION_ID}/track`),
    fetch(`/api/sessions/${SESSION_ID}/replay`),
  ]);
  try {
    if (trackResult.status === 'fulfilled' && trackResult.value.ok) {
      const geo = await trackResult.value.json();
      const feat = (geo.features || [])[0];
      if (feat && feat.geometry && feat.geometry.coordinates) {
        _sessionTrack = {
          coords: feat.geometry.coordinates,
          timestamps: (feat.properties || {}).timestamps || [],
        };
      }
    }
  } catch (_e) { /* track overlay is optional */ }
  try {
    if (replayResult.status === 'fulfilled' && replayResult.value.ok) {
      const rData = await replayResult.value.json();
      _replaySamples = (rData.samples || []).map(s => ({
        ts: new Date(s.ts),
        hdg: s.hdg, cog: s.cog, stw: s.stw, sog: s.sog,
        tws: s.tws, twa: s.twa, twd: s.twd, aws: s.aws, awa: s.awa,
      }));
    }
  } catch (_e) { /* gauge overlay is optional */ }

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

    const trackSvg = _renderTrackOverlay(m, i);
    const courseSvg = _renderCourseOverlay(m, i);
    const gaugeSvg = _renderGaugePlaceholder(i);
    const wrapId = 'yt-wrap-' + i;
    cell.innerHTML =
      '<button class="cell-dismiss" onclick="dismissCell(' + i + ')" title="Remove from comparison">&#10005;</button>'
      + '<div class="yt-wrap" id="' + wrapId + '" style="height:' + Math.max(60, videoH) + 'px">'
      + '<div id="' + divId + '" style="width:100%;height:100%"></div>'
      + trackSvg
      + courseSvg
      + gaugeSvg
      + '</div>'
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
        // Initial gauge + track dot update for the paused state
        setTimeout(_tickUpdate, 500);
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
  setTimeout(_tickUpdate, 300);
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
    _stopTrackTick();
  } else {
    _playing = true;
    _players.forEach(p => { try { p.player.playVideo(); } catch (_e) { /* not ready */ } });
    document.getElementById('play-all-btn').innerHTML = '&#9646;&#9646; Pause All';
    _startTrackTick();
  }
}

function seekAllToStart() {
  _playing = false;
  _stopTrackTick();
  document.getElementById('play-all-btn').innerHTML = '&#9654; Play All';
  _players.forEach(p => {
    p.cueSeconds = _calcCue(p.maneuver, p.nudge);
    try {
      p.player.seekTo(p.cueSeconds, true);
      p.player.pauseVideo();
    } catch (_e) { /* not ready */ }
  });
  setTimeout(_tickUpdate, 300);
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
  _stopTrackTick();
  document.getElementById('play-all-btn').innerHTML = '&#9654; Play All';
  _players.forEach(p => {
    p.cueSeconds = _calcCue(p.maneuver, p.nudge);
    try {
      p.player.seekTo(p.cueSeconds, true);
      p.player.pauseVideo();
    } catch (_e) { /* not ready */ }
  });
  setTimeout(_tickUpdate, 300);
}

// ---------------------------------------------------------------------------
// Track overlay (#570)
// ---------------------------------------------------------------------------

function _renderTrackOverlay(m, idx) {
  const track = m.track;
  if (!track || track.length < 2) return '';

  const size = 120;
  const pad = 8;

  // Compute bounds
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const p of track) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  if (!isFinite(minX)) return '';

  // Square bounds with padding
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const half = Math.max(5, Math.max(maxX - minX, maxY - minY) / 2 + 3);
  const bMinX = cx - half, bMaxX = cx + half, bMinY = cy - half, bMaxY = cy + half;

  const sx = x => pad + (x - bMinX) / (bMaxX - bMinX) * (size - 2 * pad);
  const sy = y => (size - pad) - (y - bMinY) / (bMaxY - bMinY) * (size - 2 * pad);

  // Build polyline
  const pts = track.map(p => sx(p.x).toFixed(1) + ',' + sy(p.y).toFixed(1)).join(' ');

  // Origin crosshair (maneuver start point)
  const ox = sx(0), oy = sy(0);

  // Color by rank
  const rankColors = { good: '#3db86e', bad: '#d64545', avg: '#888' };
  const color = rankColors[m.rank] || '#7eb8f7';

  const display = _trackOverlayVisible ? '' : 'display:none;';

  return '<svg class="track-overlay" id="track-svg-' + idx + '" width="' + size + '" height="' + size + '" style="' + display + '">'
    + '<rect width="' + size + '" height="' + size + '" rx="6" fill="rgba(0,0,0,.45)"/>'
    + '<polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
    + '<circle cx="' + ox + '" cy="' + oy + '" r="2.5" fill="#fff" stroke="' + color + '" stroke-width="1"/>'
    + '<text x="' + (size - pad) + '" y="' + (pad + 6) + '" text-anchor="end" font-size="7" fill="rgba(255,255,255,.5)">&#8593; wind</text>'
    + '<circle id="track-dot-' + idx + '" cx="' + ox + '" cy="' + oy + '" r="3.5" fill="#fff" stroke="rgba(0,0,0,.6)" stroke-width="1"/>'
    + '</svg>';
}

function _renderCourseOverlay(m, idx) {
  if (!_sessionTrack || !_sessionTrack.coords.length) return '';
  const coords = _sessionTrack.coords; // [lng, lat]
  if (coords.length < 2) return '';

  const size = 100;
  const pad = 6;

  // Convert lng/lat to simple x/y (Mercator-ish, fine for local scale)
  const lat0 = coords[0][1], lng0 = coords[0][0];
  const cosLat = Math.cos(lat0 * Math.PI / 180);
  const mPerDeg = 111320;
  const points = coords.map(c => ({
    x: (c[0] - lng0) * mPerDeg * cosLat,
    y: (c[1] - lat0) * mPerDeg,
  }));

  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const p of points) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  if (!isFinite(minX)) return '';

  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const half = Math.max(20, Math.max(maxX - minX, maxY - minY) / 2 * 1.1);
  const sx = x => pad + (x - (cx - half)) / (2 * half) * (size - 2 * pad);
  const sy = y => (size - pad) - (y - (cy - half)) / (2 * half) * (size - 2 * pad);

  // Downsample for SVG performance (keep every Nth point)
  const step = Math.max(1, Math.floor(points.length / 300));
  const pts = [];
  for (let i = 0; i < points.length; i += step) {
    pts.push(sx(points[i].x).toFixed(1) + ',' + sy(points[i].y).toFixed(1));
  }

  // Maneuver position marker
  let marker = '';
  if (m.lat != null && m.lon != null) {
    const mx = (m.lon - lng0) * mPerDeg * cosLat;
    const my = (m.lat - lat0) * mPerDeg;
    const msx = sx(mx), msy = sy(my);
    const rankColors = { good: '#3db86e', bad: '#d64545', avg: '#888' };
    const mc = rankColors[m.rank] || '#7eb8f7';
    marker = '<circle cx="' + msx.toFixed(1) + '" cy="' + msy.toFixed(1)
      + '" r="4" fill="' + mc + '" stroke="#fff" stroke-width="1.5"/>';
  }

  const display = _trackOverlayVisible ? '' : 'display:none;';

  return '<svg class="track-overlay course-overlay" id="course-svg-' + idx + '" width="' + size + '" height="' + size + '" style="' + display + 'bottom:auto;left:auto;top:6px;right:30px">'
    + '<rect width="' + size + '" height="' + size + '" rx="6" fill="rgba(0,0,0,.4)"/>'
    + '<polyline points="' + pts.join(' ') + '" fill="none" stroke="rgba(255,255,255,.4)" stroke-width="1" stroke-linejoin="round"/>'
    + marker
    + '</svg>';
}

function toggleTrackOverlay() {
  _trackOverlayVisible = !_trackOverlayVisible;
  const btn = document.getElementById('track-toggle-btn');
  if (btn) {
    btn.style.background = _trackOverlayVisible ? 'var(--accent-strong)' : 'var(--bg-input)';
    btn.style.color = _trackOverlayVisible ? 'var(--bg-primary)' : 'var(--text-secondary)';
    btn.style.border = _trackOverlayVisible ? 'none' : '1px solid var(--border)';
  }
  for (let i = 0; i < _allManeuvers.length; i++) {
    const svg = document.getElementById('track-svg-' + i);
    if (svg) svg.style.display = _trackOverlayVisible ? '' : 'none';
    const course = document.getElementById('course-svg-' + i);
    if (course) course.style.display = _trackOverlayVisible ? '' : 'none';
  }
}

// Update overlays based on current playback time
function _startTrackTick() {
  if (_tickInterval) return;
  _tickInterval = setInterval(_tickUpdate, 200);
}

function _stopTrackTick() {
  if (_tickInterval) { clearInterval(_tickInterval); _tickInterval = 0; }
}

function _tickUpdate() {
  for (const p of _players) {
    let videoTime;
    try { videoTime = p.player.getCurrentTime(); } catch (_e) { continue; }

    // --- Track dot update ---
    if (_trackOverlayVisible) {
      const track = p.maneuver.track;
      if (track && track.length >= 2) {
        const dot = document.getElementById('track-dot-' + p.idx);
        if (dot) {
          const currentT = videoTime - (p.maneuver.video_offset_s || 0);
          let best = track[0], bestDt = Math.abs(track[0].t - currentT);
          for (let i = 1; i < track.length; i++) {
            const dt = Math.abs(track[i].t - currentT);
            if (dt < bestDt) { bestDt = dt; best = track[i]; }
          }
          const size = 120, pad = 8;
          let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
          for (const pt of track) {
            if (pt.x < minX) minX = pt.x; if (pt.x > maxX) maxX = pt.x;
            if (pt.y < minY) minY = pt.y; if (pt.y > maxY) maxY = pt.y;
          }
          const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
          const half = Math.max(5, Math.max(maxX - minX, maxY - minY) / 2 + 3);
          const svgX = pad + (best.x - (cx - half)) / (2 * half) * (size - 2 * pad);
          const svgY = (size - pad) - (best.y - (cy - half)) / (2 * half) * (size - 2 * pad);
          dot.setAttribute('cx', svgX.toFixed(1));
          dot.setAttribute('cy', svgY.toFixed(1));
        }
      }
    }

    // --- Gauge update ---
    if (_gaugeVisible && _replaySamples && _replaySamples.length) {
      _updateGauge(p, videoTime);
    }
  }
}

// ---------------------------------------------------------------------------
// Instrument gauge overlay (#572)
// ---------------------------------------------------------------------------

function _renderGaugePlaceholder(idx) {
  if (!_replaySamples || !_replaySamples.length) return '';
  const s = 130; // gauge size
  const r = 56;  // compass radius
  const cx = s / 2, cy = s / 2;
  const display = _gaugeVisible ? '' : 'display:none;';

  // Compass ticks
  let ticks = '';
  for (let d = 0; d < 360; d += 10) {
    const rad = (d - 90) * Math.PI / 180;
    const inner = d % 30 === 0 ? r - 8 : r - 4;
    const x1 = cx + inner * Math.cos(rad), y1 = cy + inner * Math.sin(rad);
    const x2 = cx + r * Math.cos(rad), y2 = cy + r * Math.sin(rad);
    ticks += '<line x1="' + x1.toFixed(1) + '" y1="' + y1.toFixed(1)
      + '" x2="' + x2.toFixed(1) + '" y2="' + y2.toFixed(1)
      + '" stroke="rgba(255,255,255,.35)" stroke-width="' + (d % 30 === 0 ? '1.2' : '0.6') + '"/>';
  }
  // Cardinal labels
  const cardinals = [{l:'N',d:0},{l:'E',d:90},{l:'S',d:180},{l:'W',d:270}];
  let labels = '';
  for (const c of cardinals) {
    const rad = (c.d - 90) * Math.PI / 180;
    const lx = cx + (r + 7) * Math.cos(rad), ly = cy + (r + 7) * Math.sin(rad);
    labels += '<text x="' + lx.toFixed(1) + '" y="' + ly.toFixed(1)
      + '" text-anchor="middle" dominant-baseline="central" font-size="7" fill="rgba(255,255,255,.5)">' + c.l + '</text>';
  }

  return '<svg class="gauge-overlay" id="gauge-svg-' + idx + '" width="' + s + '" height="' + s + '" style="' + display + '">'
    + '<circle cx="' + cx + '" cy="' + cy + '" r="' + (r + 10) + '" fill="rgba(0,0,0,.5)"/>'
    + '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" stroke="rgba(255,255,255,.25)" stroke-width="1"/>'
    + ticks + labels
    // TWD arrow (orange) — rotated by JS
    + '<g id="gauge-twd-' + idx + '" transform="rotate(0,' + cx + ',' + cy + ')">'
    + '<line x1="' + cx + '" y1="' + (cy + r - 10) + '" x2="' + cx + '" y2="' + (cy - r + 12) + '" stroke="#f59e0b" stroke-width="2" stroke-linecap="round"/>'
    + '<polygon points="' + cx + ',' + (cy - r + 8) + ' ' + (cx - 4) + ',' + (cy - r + 16) + ' ' + (cx + 4) + ',' + (cy - r + 16) + '" fill="#f59e0b"/>'
    + '</g>'
    // AWA arrow (blue) — rotated by JS
    + '<g id="gauge-awa-' + idx + '" transform="rotate(0,' + cx + ',' + cy + ')">'
    + '<line x1="' + cx + '" y1="' + cy + '" x2="' + cx + '" y2="' + (cy - r + 18) + '" stroke="#60a5fa" stroke-width="1.8" stroke-linecap="round"/>'
    + '<polygon points="' + cx + ',' + (cy - r + 14) + ' ' + (cx - 3) + ',' + (cy - r + 21) + ' ' + (cx + 3) + ',' + (cy - r + 21) + '" fill="#60a5fa"/>'
    + '</g>'
    // COG line (white dashed)
    + '<g id="gauge-cog-' + idx + '" transform="rotate(0,' + cx + ',' + cy + ')">'
    + '<line x1="' + cx + '" y1="' + cy + '" x2="' + cx + '" y2="' + (cy - r + 6) + '" stroke="rgba(255,255,255,.6)" stroke-width="1" stroke-dasharray="3,2"/>'
    + '</g>'
    // Boat icon (center)
    + '<polygon points="' + cx + ',' + (cy - 6) + ' ' + (cx - 4) + ',' + (cy + 5) + ' ' + (cx + 4) + ',' + (cy + 5) + '" fill="#fff" stroke="rgba(0,0,0,.4)" stroke-width="0.5"/>'
    // HDG readout (top)
    + '<rect x="' + (cx - 14) + '" y="2" width="28" height="12" rx="2" fill="rgba(0,0,0,.7)"/>'
    + '<text id="gauge-hdg-' + idx + '" x="' + cx + '" y="11" text-anchor="middle" font-size="8" font-weight="600" font-family="monospace" fill="#fff">---</text>'
    // BSP readout (left)
    + '<text id="gauge-bsp-' + idx + '" x="8" y="' + (cy + 2) + '" text-anchor="start" font-size="9" font-weight="700" font-family="monospace" fill="#3db86e">-.-</text>'
    + '<text x="8" y="' + (cy + 10) + '" text-anchor="start" font-size="5" fill="rgba(255,255,255,.5)">BSP</text>'
    // SOG readout (right)
    + '<text id="gauge-sog-' + idx + '" x="' + (s - 8) + '" y="' + (cy + 2) + '" text-anchor="end" font-size="9" font-weight="700" font-family="monospace" fill="#fff">-.-</text>'
    + '<text x="' + (s - 8) + '" y="' + (cy + 10) + '" text-anchor="end" font-size="5" fill="rgba(255,255,255,.5)">SOG</text>'
    // TWS readout (bottom-left, orange)
    + '<text id="gauge-tws-' + idx + '" x="12" y="' + (s - 6) + '" text-anchor="start" font-size="8" font-weight="600" font-family="monospace" fill="#f59e0b">--</text>'
    + '<text x="12" y="' + (s - 14) + '" text-anchor="start" font-size="5" fill="rgba(255,255,255,.5)">TWS</text>'
    // AWS readout (bottom-right, blue)
    + '<text id="gauge-aws-' + idx + '" x="' + (s - 12) + '" y="' + (s - 6) + '" text-anchor="end" font-size="8" font-weight="600" font-family="monospace" fill="#60a5fa">--</text>'
    + '<text x="' + (s - 12) + '" y="' + (s - 14) + '" text-anchor="end" font-size="5" fill="rgba(255,255,255,.5)">AWS</text>'
    + '</svg>';
}

function _updateGauge(p, videoTime) {
  // Convert video time to UTC
  const mTs = _parseUtcMs(p.maneuver.ts);
  if (!mTs) return;
  const offsetS = p.maneuver.video_offset_s || 0;
  const utcMs = mTs + (videoTime - offsetS) * 1000;

  // Binary search for nearest sample
  const sample = _sampleAtTime(utcMs);
  if (!sample) return;

  const idx = p.idx;
  const cx = 65, cy = 65; // gauge center

  // Update HDG
  const hdgEl = document.getElementById('gauge-hdg-' + idx);
  if (hdgEl) hdgEl.textContent = sample.hdg != null ? Math.round(sample.hdg) : '---';

  // Update BSP
  const bspEl = document.getElementById('gauge-bsp-' + idx);
  if (bspEl) bspEl.textContent = sample.stw != null ? sample.stw.toFixed(1) : '-.-';

  // Update SOG
  const sogEl = document.getElementById('gauge-sog-' + idx);
  if (sogEl) sogEl.textContent = sample.sog != null ? sample.sog.toFixed(1) : '-.-';

  // Update TWS
  const twsEl = document.getElementById('gauge-tws-' + idx);
  if (twsEl) twsEl.textContent = sample.tws != null ? sample.tws.toFixed(0) : '--';

  // Update AWS
  const awsEl = document.getElementById('gauge-aws-' + idx);
  if (awsEl) awsEl.textContent = sample.aws != null ? sample.aws.toFixed(0) : '--';

  // Rotate TWD arrow: show wind direction relative to heading
  // TWD is compass direction wind comes FROM; on the gauge, HDG is up (north=up, rotated)
  // We want the arrow to point in the direction wind blows FROM, relative to the boat heading
  const twdG = document.getElementById('gauge-twd-' + idx);
  if (twdG && sample.twd != null && sample.hdg != null) {
    const relTwd = ((sample.twd - sample.hdg) + 360) % 360;
    twdG.setAttribute('transform', 'rotate(' + relTwd.toFixed(1) + ',' + cx + ',' + cy + ')');
  }

  // Rotate AWA arrow: boat-relative, so direct rotation from top
  const awaG = document.getElementById('gauge-awa-' + idx);
  if (awaG && sample.awa != null) {
    awaG.setAttribute('transform', 'rotate(' + sample.awa.toFixed(1) + ',' + cx + ',' + cy + ')');
  }

  // Rotate COG line relative to heading
  const cogG = document.getElementById('gauge-cog-' + idx);
  if (cogG && sample.cog != null && sample.hdg != null) {
    const relCog = ((sample.cog - sample.hdg) + 360) % 360;
    cogG.setAttribute('transform', 'rotate(' + relCog.toFixed(1) + ',' + cx + ',' + cy + ')');
  }
}

function _parseUtcMs(iso) {
  if (!iso) return null;
  let s = iso.replace(' ', 'T');
  if (!s.endsWith('Z') && !s.includes('+')) s += 'Z';
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d.getTime();
}

function _sampleAtTime(utcMs) {
  if (!_replaySamples || !_replaySamples.length) return null;
  let lo = 0, hi = _replaySamples.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (_replaySamples[mid].ts.getTime() <= utcMs) lo = mid;
    else hi = mid - 1;
  }
  return _replaySamples[lo];
}

function toggleGaugeOverlay() {
  _gaugeVisible = !_gaugeVisible;
  const btn = document.getElementById('gauge-toggle-btn');
  if (btn) {
    btn.style.background = _gaugeVisible ? 'var(--accent-strong)' : 'var(--bg-input)';
    btn.style.color = _gaugeVisible ? 'var(--bg-primary)' : 'var(--text-secondary)';
    btn.style.border = _gaugeVisible ? 'none' : '1px solid var(--border)';
  }
  for (let i = 0; i < _allManeuvers.length; i++) {
    const svg = document.getElementById('gauge-svg-' + i);
    if (svg) svg.style.display = _gaugeVisible ? '' : 'none';
  }
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
    let s = iso.replace(' ', 'T');
    if (!s.endsWith('Z') && !s.includes('+')) s += 'Z';
    const d = new Date(s);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch (_e) { return ''; }
}

function _esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
