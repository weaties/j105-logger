/* compare.js — Synced multi-video maneuver comparison (#565)
 *
 * Loads maneuver data from the compare API, creates one YouTube IFrame
 * player per maneuver, and wires them to a common play/pause control.
 * Each player is cued to [maneuver_ts - preroll] in video time.
 */

'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const SESSION_ID = document.getElementById('app-config').dataset.sessionId;
let _players = [];   // { player: YT.Player, cueSeconds: number, maneuver: object }
let _playing = false;
let _prerollS = 10;
let _ytReady = false;

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

(async function init() {
  _loadYouTubeAPI();
  const ids = new URLSearchParams(window.location.search).get('ids');
  if (!ids) {
    _showEmpty();
    return;
  }
  const resp = await fetch(`/api/sessions/${SESSION_ID}/maneuvers/compare?ids=${ids}`);
  if (!resp.ok) {
    _showEmpty();
    return;
  }
  const data = await resp.json();
  const maneuvers = data.maneuvers || [];
  const videoSync = data.video_sync;
  if (!maneuvers.length || !videoSync) {
    _showEmpty();
    return;
  }

  // Filter to maneuvers that have video coverage
  const withVideo = maneuvers.filter(m => m.youtube_url);
  if (!withVideo.length) {
    _showEmpty();
    return;
  }

  _renderGrid(withVideo, videoSync);
  document.getElementById('compare-subtitle').textContent =
    withVideo.length + ' maneuver' + (withVideo.length > 1 ? 's' : '');
  document.getElementById('compare-controls').style.display = '';
})();

// ---------------------------------------------------------------------------
// YouTube IFrame API
// ---------------------------------------------------------------------------

function _loadYouTubeAPI() {
  if (window.YT && window.YT.Player) {
    _ytReady = true;
    return;
  }
  const tag = document.createElement('script');
  tag.src = 'https://www.youtube.com/iframe_api';
  document.head.appendChild(tag);
}

window.onYouTubeIframeAPIReady = function () {
  _ytReady = true;
  _createPendingPlayers();
};

let _pendingPlayers = []; // queued until YT API ready

function _createPendingPlayers() {
  for (const p of _pendingPlayers) {
    _createPlayer(p.divId, p.videoId, p.cueSeconds, p.maneuver);
  }
  _pendingPlayers = [];
}

// ---------------------------------------------------------------------------
// Grid rendering
// ---------------------------------------------------------------------------

function _renderGrid(maneuvers, videoSync) {
  const grid = document.getElementById('compare-grid');

  for (let i = 0; i < maneuvers.length; i++) {
    const m = maneuvers[i];
    const cueSeconds = Math.max(0, (m.video_offset_s || 0) - _prerollS);

    const cell = document.createElement('div');
    cell.className = 'compare-cell';
    const divId = 'yt-compare-' + i;
    const typeClass = 'badge-' + (m.type || 'maneuver');
    const dirHint = _directionHint(m);
    const dur = m.duration_sec != null ? m.duration_sec.toFixed(1) + 's' : '';
    const loss = m.loss_kts != null ? m.loss_kts.toFixed(2) + ' kt dip' : '';
    const distLoss = m.distance_loss_m != null ? Math.round(m.distance_loss_m) + ' m' : '';
    const rank = m.rank ? (' <span style="color:' + _rankColor(m.rank) + '">&#9679; ' + m.rank + '</span>') : '';
    const elapsed = _fmtElapsed(m.ts);
    const bsp = (m.entry_bsp != null ? m.entry_bsp.toFixed(1) : '?') + '&#8594;' + (m.exit_bsp != null ? m.exit_bsp.toFixed(1) : '?');
    const turn = m.turn_angle_deg != null ? Math.abs(Math.round(m.turn_angle_deg)) + '&deg;' : '';
    cell.innerHTML =
      '<div class="yt-wrap" id="' + divId + '"></div>'
      + '<div class="cell-label">'
      + '<b class="' + typeClass + '">' + _esc(m.type || 'maneuver') + '</b>'
      + dirHint + rank
      + ' <span style="font-variant-numeric:tabular-nums">' + elapsed + '</span>'
      + (turn ? ' &middot; ' + turn : '')
      + ' &middot; ' + bsp + ' kt'
      + (dur ? ' &middot; ' + dur : '')
      + (loss ? ' &middot; ' + loss : '')
      + (distLoss ? ' &middot; ' + distLoss : '')
      + '</div>';
    grid.appendChild(cell);

    const entry = { divId, videoId: videoSync.video_id, cueSeconds, maneuver: m };
    if (_ytReady) {
      _createPlayer(divId, videoSync.video_id, cueSeconds, m);
    } else {
      _pendingPlayers.push(entry);
    }
  }
}

function _createPlayer(divId, videoId, cueSeconds, maneuver) {
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
  _players.push({ player, cueSeconds, maneuver });
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
  _players.forEach(p => {
    p.cueSeconds = Math.max(0, (p.maneuver.video_offset_s || 0) - _prerollS);
    try {
      p.player.seekTo(p.cueSeconds, true);
      p.player.pauseVideo();
    } catch (_e) { /* not ready */ }
  });
  _playing = false;
  document.getElementById('play-all-btn').innerHTML = '&#9654; Play All';
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
      ? ' <span style="color:var(--text-secondary);font-size:.72rem">S&#8594;P</span>'
      : ' <span style="color:var(--text-secondary);font-size:.72rem">P&#8594;S</span>';
  }
  return '';
}

function _rankColor(rank) {
  const m = { good: 'var(--success)', bad: 'var(--error)', avg: 'var(--text-secondary)' };
  return m[rank] || 'var(--text-secondary)';
}

function _fmtElapsed(iso) {
  // Simple elapsed from session start — we don't have _session here,
  // so just show the time portion
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
