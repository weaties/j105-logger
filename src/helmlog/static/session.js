/* session.js — Session detail page logic */

const cfg = document.getElementById('app-config');
const SESSION_ID = cfg.dataset.sessionId;
initGrafana(cfg.dataset.grafanaPort, cfg.dataset.grafanaUid);

let _session = null;
let _map = null;
let _trackData = null; // {latLngs, timestamps (as Date), line, cursor}
let _videoSync = null; // {syncUtc (Date), syncOffsetS, durationS, player}
let _ytReady = false;
let _syncTimer = null;

// ---------------------------------------------------------------------------
// Playback clock — single source of truth for the session timeline (#446)
//
// Every surface (map, video, audio, transcript) is both a producer (calls
// setPosition when the user interacts with it) and a consumer (renders the
// current position). Producers go through one entry point so there is exactly
// one clock. Echo events from media elements are debounced via _seekingUntil.
// ---------------------------------------------------------------------------

const _playClock = {
  positionUtc: null, // current position as a Date on session UTC timeline
  state: 'idle', // idle | playing | paused | seeking | ended
  consumers: [], // [{name, render(utc)}]
  seekingUntil: 0, // performance.now() ms; ignore echo events until this time
  tickTimer: null,
  tickAnchorUtc: null, // UTC at last tick anchor
  tickAnchorPerf: 0, // performance.now() at last tick anchor
};

function _clockNowMs() { return performance.now(); }

function registerSurface(name, render) {
  _playClock.consumers.push({name, render});
}

function setPosition(utc, opts) {
  if (!utc) return;
  const date = utc instanceof Date ? utc : new Date(utc);
  if (isNaN(date.getTime())) return;
  _playClock.positionUtc = date;
  // Suppress media echo events for a brief window after a programmatic seek
  _playClock.seekingUntil = _clockNowMs() + 200;
  // Re-anchor the playing tick on the new position
  _playClock.tickAnchorUtc = date;
  _playClock.tickAnchorPerf = _clockNowMs();
  const source = (opts && opts.source) || null;
  for (const c of _playClock.consumers) {
    if (c.name === source) continue; // don't echo back to producer
    try { c.render(date); } catch (e) { /* never let one surface break others */ }
  }
}

function _isEchoEvent() {
  return _clockNowMs() < _playClock.seekingUntil;
}

function _startPlayTick() {
  _stopPlayTick();
  _playClock.tickAnchorUtc = _playClock.positionUtc;
  _playClock.tickAnchorPerf = _clockNowMs();
  _playClock.state = 'playing';
  _playClock.tickTimer = setInterval(() => {
    if (!_playClock.tickAnchorUtc) return;
    const elapsedMs = _clockNowMs() - _playClock.tickAnchorPerf;
    const utc = new Date(_playClock.tickAnchorUtc.getTime() + elapsedMs);
    _playClock.positionUtc = utc;
    for (const c of _playClock.consumers) {
      try { c.render(utc); } catch (e) { /* swallow */ }
    }
  }, 100);
}

function _stopPlayTick() {
  if (_playClock.tickTimer) {
    clearInterval(_playClock.tickTimer);
    _playClock.tickTimer = null;
  }
  _playClock.state = 'paused';
}

let _maneuvers = []; // loaded maneuver list
let _maneuverMarkers = []; // Leaflet markers for maneuvers
let _transcriptId = null; // transcript ID for tuning extraction
let _tuningSegmentAudio = null; // shared <audio> for segment playback
let _tuningSegmentTimer = null; // timeupdate stop timer
let _transcriptAudio = null; // shared <audio> for transcript segment playback
let _transcriptBlocks = []; // merged transcript blocks for highlight tracking
let _speakerMap = {}; // speaker_map from API (crew assignments)

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

async function init() {
  await initTimezone();

  const r = await fetch('/api/sessions/' + SESSION_ID + '/detail');
  if (!r.ok) {
    document.getElementById('session-name').textContent = 'Session not found';
    return;
  }
  _session = await r.json();

  renderHeader();
  // Load track and videos in parallel, then wire up sync
  await Promise.all([loadTrack(), loadVideoPlayer()]);
  loadManeuvers();
  loadVideos();
  if (_session.has_wind_field) loadWindField();
  if (_session.type !== 'debrief') {
    loadResults();
    loadCrew();
    loadSails();
    loadBoatSettings();
    loadNotes();
    if (_session.end_utc) loadPolar();
    loadAnalysis();
  }
  if (_session.has_audio && _session.audio_session_id) {
    loadTranscript();
    loadAudio();
  }
  await loadDiscussion();
  _checkThreadHash();
  loadSharing();
  loadMatch();
  renderExports();
  renderDangerZone();
}

// ---------------------------------------------------------------------------
// Danger zone — session deletion (#409)
// ---------------------------------------------------------------------------

function renderDangerZone() {
  const role = cfg.dataset.userRole;
  if (role !== 'admin') return;
  if (!_session.end_utc) return; // active session — cannot delete
  document.getElementById('danger-zone').style.display = '';
}

async function deleteSession() {
  const name = _session.name || 'this session';
  if (!confirm('Delete "' + name + '"?\n\nThis will permanently remove all data, audio, and files. This cannot be undone.')) return;
  const btn = document.getElementById('delete-session-btn');
  btn.disabled = true;
  btn.textContent = 'Deleting\u2026';
  const r = await fetch('/api/sessions/' + SESSION_ID, { method: 'DELETE' });
  if (r.ok) {
    window.location.href = '/history';
  } else {
    btn.disabled = false;
    btn.textContent = 'Delete Session';
    const data = await r.json().catch(() => null);
    alert('Delete failed: ' + (data && data.detail ? data.detail : r.statusText));
  }
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

function renderHeader() {
  const s = _session;
  const typeClass = s.type === 'race' ? 'badge-race'
    : s.type === 'practice' ? 'badge-practice'
    : s.type === 'synthesized' ? 'badge-synthesized'
    : 'badge-debrief';
  const badge = '<span class="badge ' + typeClass + '">' + s.type.toUpperCase() + '</span>';
  const peerBadge = s.peer_fingerprint
    ? '<span class="badge badge-peer" title="Peer boat">PEER</span>'
    : '';
  const matchBadge = s.match_status === 'confirmed'
    ? '<span class="badge badge-practice" title="Co-op matched">MATCHED</span>'
    : s.match_status === 'candidate'
    ? '<span class="badge badge-debrief" title="Pending match">PENDING</span>'
    : '';
  const displayName = s.shared_name || s.name;
  document.getElementById('session-name').innerHTML = esc(displayName) + badge + peerBadge + matchBadge;

  const start = fmtTime(s.start_utc);
  const end = s.end_utc ? fmtTime(s.end_utc) : 'in progress';
  const dur = (s.end_utc && s.duration_s != null) ? ' (' + fmtDuration(Math.round(s.duration_s)) + ')' : '';
  let meta = s.date + ' &middot; ' + start + ' &rarr; ' + end + dur;
  if (s.shared_name) meta += '<br><span style="font-size:.72rem;color:var(--text-secondary)">Local: ' + esc(s.name) + '</span>';
  document.getElementById('session-meta').innerHTML = meta;
}

// ---------------------------------------------------------------------------
// Track map
// ---------------------------------------------------------------------------

async function loadTrack() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/track');
  const geojson = await r.json();
  if (!geojson.features || !geojson.features.length) return;

  const container = document.getElementById('track-container');
  container.style.display = '';

  _map = L.map('track-map');
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap', maxZoom: 18,
  }).addTo(_map);

  const feature = geojson.features[0];
  const coords = feature.geometry.coordinates;
  const rawTimestamps = feature.properties.timestamps || [];
  const latLngs = coords.map(c => [c[1], c[0]]);
  const timestamps = rawTimestamps.map(t => new Date(t.endsWith('Z') || t.includes('+') ? t : t + 'Z'));
  const trackColor = cssVar('--accent-strong');
  const line = L.polyline(latLngs, {color: trackColor, weight: 4}).addTo(_map);

  const successColor = cssVar('--success');
  const dangerColor = cssVar('--danger');
  const warningColor = cssVar('--warning');
  L.circleMarker(latLngs[0], {radius: 6, color: successColor, fillColor: successColor, fillOpacity: 1})
    .addTo(_map).bindPopup('Start');
  L.circleMarker(latLngs[latLngs.length - 1], {radius: 6, color: dangerColor, fillColor: dangerColor, fillOpacity: 1})
    .addTo(_map).bindPopup('Finish');

  const cursor = L.circleMarker([0, 0], {
    radius: 7, color: warningColor, fillColor: warningColor, fillOpacity: 1, weight: 2,
  });

  _trackData = {latLngs, timestamps, line, cursor};

  // Map is a consumer: render the cursor at the requested UTC
  registerSurface('map', function(utc) {
    if (!_trackData) return;
    const idx = _indexForUtc(utc);
    _moveCursorToIndex(idx);
    _updateBoatSettingsForUtc(_utcForIndex(idx));
  });

  // Click track → seek the playback clock (which then seeks video, audio, etc.)
  line.on('click', function(e) {
    const idx = _nearestIndex(e.latlng);
    const utc = _utcForIndex(idx);
    if (utc) setPosition(utc, {source: 'map'});
    // Map producer still updates its own cursor immediately
    _moveCursorToIndex(idx);
  });

  // Right-click track → start discussion at that point
  line.on('contextmenu', function(e) {
    L.DomEvent.preventDefault(e);
    const idx = _nearestIndex(e.latlng);
    const utc = _utcForIndex(idx);
    if (utc) {
      _moveCursorToIndex(idx);
      showNewThreadForm(utc.toISOString());
      document.getElementById('discussion-card').scrollIntoView({behavior: 'smooth', block: 'start'});
    }
  });

  _map.fitBounds(line.getBounds(), {padding: [20, 20]});
  document.getElementById('track-hint').textContent = 'Click track to seek \u00b7 Right-click to start a discussion at that point';
}

function _nearestIndex(latlng) {
  if (!_trackData) return 0;
  let minDist = Infinity, nearIdx = 0;
  for (let i = 0; i < _trackData.latLngs.length; i++) {
    const d = _map.latLngToLayerPoint(_trackData.latLngs[i])
      .distanceTo(_map.latLngToLayerPoint(latlng));
    if (d < minDist) { minDist = d; nearIdx = i; }
  }
  return nearIdx;
}

function _moveCursorToIndex(idx) {
  if (!_trackData) return;
  _trackData.cursor.setLatLng(_trackData.latLngs[idx]).addTo(_map);
}

function _indexForUtc(utcDate) {
  if (!_trackData || !_trackData.timestamps.length) return 0;
  const t = utcDate.getTime();
  // Binary-ish search for nearest timestamp
  let best = 0, bestDiff = Math.abs(_trackData.timestamps[0].getTime() - t);
  for (let i = 1; i < _trackData.timestamps.length; i++) {
    const diff = Math.abs(_trackData.timestamps[i].getTime() - t);
    if (diff < bestDiff) { bestDiff = diff; best = i; }
    if (_trackData.timestamps[i].getTime() > t) break; // timestamps are sorted
  }
  return best;
}

function _utcForIndex(idx) {
  if (!_trackData || !_trackData.timestamps.length) return null;
  return _trackData.timestamps[Math.min(idx, _trackData.timestamps.length - 1)];
}

// ---------------------------------------------------------------------------
// YouTube IFrame Player
// ---------------------------------------------------------------------------

async function loadVideoPlayer() {
  const vr = await fetch('/api/sessions/' + SESSION_ID + '/videos');
  const videos = await vr.json();
  if (!videos.length) return;

  // Use first video with a video_id
  const vid = videos.find(v => v.video_id) || videos[0];
  if (!vid || !vid.video_id) return;

  _videoSync = {
    syncUtc: new Date(vid.sync_utc),
    syncOffsetS: vid.sync_offset_s || 0,
    durationS: vid.duration_s || 0,
    player: null,
    videoId: vid.video_id,
    allVideos: videos,
    activeIdx: videos.indexOf(vid),
  };

  const container = document.getElementById('video-container');
  container.style.display = '';

  // Render video switcher if multiple videos
  if (videos.length > 1) {
    const switcher = document.getElementById('video-switcher');
    switcher.innerHTML = videos.map((v, i) => {
      const label = v.label || v.title || ('Video ' + (i + 1));
      const cls = i === _videoSync.activeIdx ? 'filter-btn active' : 'filter-btn';
      return '<button class="' + cls + '" onclick="switchVideo(' + i + ')">' + esc(label) + '</button>';
    }).join('');
  }

  // Load YouTube IFrame API
  const tag = document.createElement('script');
  tag.src = 'https://www.youtube.com/iframe_api';
  document.head.appendChild(tag);
}

// YouTube API calls this global function when ready
function onYouTubeIframeAPIReady() {
  _ytReady = true;
  if (_videoSync) _createPlayer(_videoSync.videoId);
}

function _createPlayer(videoId) {
  if (_videoSync.player) {
    _videoSync.player.loadVideoById(videoId);
    _ensureWatchOnYoutubeLink();
    return;
  }
  // Use the standard YT.Player div-based instantiation. The manual iframe
  // approach broke YouTube's embedder identity verification (Error 153).
  // 360 video panning is NOT supported in YouTube iframe embeds for
  // third-party domains regardless of `allow` attributes — it only works on
  // youtube.com itself and in the YouTube app. We expose a "Watch on YouTube"
  // link below the player as the workaround for spherical videos.
  _videoSync.player = new YT.Player('yt-player', {
    height: '100%',
    width: '100%',
    videoId: videoId,
    playerVars: {
      modestbranding: 1,
      rel: 0,
      enablejsapi: 1,
      origin: location.origin,
    },
    events: {
      onReady: _onVideoReady,
      onStateChange: _onPlayerStateChange,
    },
  });
  _ensureWatchOnYoutubeLink();
}

// Render the "Watch on YouTube" link once. The URL is computed live in the
// click handler so it always reflects the currently selected video and its
// current playhead — the old implementation baked a stale videoId + t=0 into
// the href at player-create time and never refreshed on switchVideo().
function _ensureWatchOnYoutubeLink() {
  if (document.getElementById('yt-watch-on-youtube')) return;
  const linkBar = document.createElement('div');
  linkBar.id = 'yt-watch-on-youtube';
  linkBar.style.cssText = 'margin-top:6px;text-align:right;font-size:.75rem';
  const container = document.getElementById('video-container');
  if (container) container.appendChild(linkBar);
  linkBar.innerHTML = '<a href="#" rel="noopener" style="color:var(--accent);text-decoration:none" title="Open in YouTube for 360° panning controls" onclick="return _openWatchOnYoutube(event)">Watch on YouTube &#8599;</a>';
}

function _openWatchOnYoutube(ev) {
  if (ev && ev.preventDefault) ev.preventDefault();
  if (!_videoSync || !_videoSync.videoId) return false;
  let t = 0;
  try {
    if (_videoSync.player && _videoSync.player.getCurrentTime) {
      t = Math.floor(_videoSync.player.getCurrentTime() || 0);
    }
  } catch (e) { /* ignore */ }
  const url = 'https://www.youtube.com/watch?v=' + encodeURIComponent(_videoSync.videoId) + (t > 0 ? '&t=' + t + 's' : '');
  window.open(url, '_blank', 'noopener');
  return false;
}

function _onVideoReady() {
  // Video is a consumer: seek to the requested UTC if it's within range
  registerSurface('video', function(utc) {
    if (!_videoSync || !_videoSync.player || !_videoSync.player.seekTo) return;
    const offset = _utcToVideoOffset(utc);
    if (offset === null || offset < 0) return;
    if (_videoSync.durationS && offset > _videoSync.durationS) return;
    _videoSync.player.seekTo(offset, true);
  });
}

function switchVideo(idx) {
  const videos = _videoSync.allVideos;
  if (idx < 0 || idx >= videos.length) return;
  _videoSync.activeIdx = idx;
  const vid = videos[idx];
  _videoSync.syncUtc = new Date(vid.sync_utc);
  _videoSync.syncOffsetS = vid.sync_offset_s || 0;
  _videoSync.durationS = vid.duration_s || 0;
  _videoSync.videoId = vid.video_id;

  // Update switcher buttons
  document.querySelectorAll('#video-switcher .filter-btn').forEach((btn, i) => {
    btn.classList.toggle('active', i === idx);
  });

  if (_videoSync.player && _videoSync.player.loadVideoById) {
    _videoSync.player.loadVideoById(vid.video_id);
  }
}

function _onPlayerStateChange(event) {
  // YT.PlayerState.PLAYING = 1, PAUSED = 2, ENDED = 0, BUFFERING = 3
  if (event.data === 1) {
    _stopSyncTimer();
    // Treat YT play as a producer: anchor the clock to the current video time
    if (typeof _videoSync.player.getCurrentTime === 'function') {
      const utc = _videoOffsetToUtc(_videoSync.player.getCurrentTime());
      if (utc) {
        _playClock.positionUtc = utc;
        // Don't fire echo to video — but other surfaces should follow
        setPosition(utc, {source: 'video'});
      }
    }
    // Drive a 2 Hz tick from the YT player so map/transcript follow during play
    _syncTimer = setInterval(_videoTick, 500);
  } else {
    _stopSyncTimer();
    _videoTick();
  }
}

function _stopSyncTimer() {
  if (_syncTimer) { clearInterval(_syncTimer); _syncTimer = null; }
}

function _videoTick() {
  if (!_videoSync || !_videoSync.player) return;
  if (typeof _videoSync.player.getCurrentTime !== 'function') return;
  const utc = _videoOffsetToUtc(_videoSync.player.getCurrentTime());
  if (!utc) return;
  // Treat as a non-seek update: update other surfaces but not the video itself
  _playClock.positionUtc = utc;
  for (const c of _playClock.consumers) {
    if (c.name === 'video') continue;
    try { c.render(utc); } catch (e) { /* swallow */ }
  }
}

// Convert video playback seconds → UTC
function _videoOffsetToUtc(videoSeconds) {
  if (!_videoSync) return null;
  // videoSeconds = syncOffsetS + (utc - syncUtc) in seconds
  // so utc = syncUtc + (videoSeconds - syncOffsetS) * 1000
  const ms = _videoSync.syncUtc.getTime() + (videoSeconds - _videoSync.syncOffsetS) * 1000;
  return new Date(ms);
}

// Convert UTC → video playback seconds
function _utcToVideoOffset(utcDate) {
  if (!_videoSync) return null;
  return _videoSync.syncOffsetS + (utcDate.getTime() - _videoSync.syncUtc.getTime()) / 1000;
}

// Seek video to the timestamp at track index
function _seekVideoToIndex(idx) {
  const utc = _utcForIndex(idx);
  if (!utc || !_videoSync || !_videoSync.player) return;
  const offset = _utcToVideoOffset(utc);
  if (offset === null || offset < 0) return;
  if (typeof _videoSync.player.seekTo === 'function') {
    _videoSync.player.seekTo(offset, true);
  }
}

// ---------------------------------------------------------------------------
// Section toggle
// ---------------------------------------------------------------------------

const _collapsed = {'boat-settings': true};

function toggleSection(name) {
  const body = document.getElementById(name + '-body');
  const toggle = document.getElementById(name + '-toggle');
  if (!body) return;
  _collapsed[name] = !_collapsed[name];
  body.style.display = _collapsed[name] ? 'none' : '';
  if (toggle) toggle.innerHTML = _collapsed[name] ? '&#9654;' : '&#9660;';
}

// ---------------------------------------------------------------------------
// Videos (list/add/delete — below the player)
// ---------------------------------------------------------------------------

async function loadVideos() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/videos');
  const videos = await r.json();
  const card = document.getElementById('videos-card');
  const body = document.getElementById('videos-body');

  if (!videos.length && _session.type === 'debrief') return;
  card.style.display = '';

  if (videos.length) {
    body.innerHTML = videos.map(v => {
      const lbl = v.label ? '<b>' + esc(v.label) + '</b> — ' : '';
      const ttl = esc(v.title || v.youtube_url).substring(0, 60);
      const link = '<a href="' + esc(v.youtube_url) + '" target="_blank" style="color:var(--accent)">' + ttl + '</a>';
      const del = '<button onclick="deleteVideo(' + v.id + ')" style="color:var(--danger);background:none;border:none;cursor:pointer;font-size:.8rem;margin-left:8px">&#10005;</button>';
      return '<div style="margin-bottom:4px">' + lbl + link + del + '</div>';
    }).join('');
  } else {
    body.innerHTML = '<span style="color:var(--text-secondary)">No videos linked</span>';
  }
  body.innerHTML += _videoAddForm();
}

function _videoAddForm() {
  const startUtc = _session.start_utc || '';
  const defaultSync = startUtc ? new Date(startUtc).toISOString().substring(0, 19) : '';
  return '<div id="video-add-form" style="display:none;margin-top:8px">'
    + '<input id="video-url" class="field" placeholder="YouTube URL" style="width:100%;margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<input id="video-label" class="field" placeholder="Label (e.g. Bow cam)" style="width:100%;margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<div style="font-size:.72rem;color:var(--text-secondary);margin-bottom:2px">Sync calibration (optional):</div>'
    + '<input id="video-sync-utc" class="field" type="datetime-local" step="1" value="' + defaultSync + '" style="width:100%;margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<input id="video-sync-pos" class="field" placeholder="Video position (mm:ss)" style="width:100%;margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<button class="btn-export" style="background:var(--accent-strong);color:var(--bg-primary);border-color:var(--accent-strong)" onclick="submitAddVideo()">Add Video</button>'
    + ' <button onclick="document.getElementById(\'video-add-form\').style.display=\'none\'" style="background:none;border:none;color:var(--text-secondary);cursor:pointer;font-size:.82rem">Cancel</button>'
    + '</div>'
    + '<button onclick="document.getElementById(\'video-add-form\').style.display=\'\'" style="font-size:.78rem;color:var(--accent);background:none;border:none;cursor:pointer;padding:4px 0;margin-top:4px">+ Add Video</button>';
}

async function submitAddVideo() {
  const url = document.getElementById('video-url').value.trim();
  const label = document.getElementById('video-label').value.trim();
  const syncUtcVal = document.getElementById('video-sync-utc').value;
  const syncPosVal = document.getElementById('video-sync-pos').value.trim();
  if (!url) { alert('YouTube URL is required'); return; }
  const syncUtc = syncUtcVal ? (syncUtcVal.includes('Z') ? syncUtcVal : syncUtcVal + 'Z') : new Date().toISOString();
  const syncOffsetS = syncPosVal ? parseVideoPosition(syncPosVal) : 0;
  if (syncOffsetS === null) { alert('Video position must be mm:ss or seconds'); return; }
  const resp = await fetch('/api/sessions/' + SESSION_ID + '/videos', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({youtube_url: url, label, sync_utc: syncUtc, sync_offset_s: syncOffsetS})
  });
  if (!resp.ok) { alert('Failed: ' + resp.status); return; }
  // Reload everything to pick up new video in player
  location.reload();
}

async function deleteVideo(videoId) {
  if (!confirm('Remove this video link?')) return;
  await fetch('/api/videos/' + videoId, {method: 'DELETE'});
  location.reload();
}

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------

let _pickerBoats = null;

async function loadResults() {
  const card = document.getElementById('results-card');
  card.style.display = '';
  const body = document.getElementById('results-body');
  const r = await fetch('/api/sessions/' + SESSION_ID + '/results');
  const results = await r.json();

  let html = '<div id="results-list">';
  html += results.map(res => {
    const name = esc(res.sail_number + (res.boat_name ? ' — ' + res.boat_name : ''));
    const dnfCls = res.dnf ? ' active-dnf' : '';
    const dnsCls = res.dns ? ' active-dns' : '';
    return '<div class="results-row">'
      + '<span class="results-place">' + res.place + '.</span>'
      + '<span class="results-boat">' + name + '</span>'
      + '<div class="results-flags">'
      + '<button class="flag-btn' + dnfCls + '" onclick="toggleFlag(' + res.place + ',' + res.boat_id + ',' + (!res.dnf) + ',' + res.dns + ')">DNF</button>'
      + '<button class="flag-btn' + dnsCls + '" onclick="toggleFlag(' + res.place + ',' + res.boat_id + ',' + res.dnf + ',' + (!res.dns) + ')">DNS</button>'
      + '</div>'
      + '<button class="btn-del-result" onclick="deleteResult(' + res.id + ')">&#10005;</button>'
      + '</div>';
  }).join('');
  html += '</div>';

  const nextPlace = results.length + 1;
  html += '<div class="results-row" style="border-bottom:none;margin-top:4px">'
    + '<span class="results-place">' + nextPlace + '.</span>'
    + '<div style="position:relative;flex:1">'
    + '<input class="boat-picker-input" id="picker-input" placeholder="Search boat\u2026" autocomplete="off"'
    + ' oninput="filterBoats(this.value)" onfocus="openPicker()" onblur="closePicker()"/>'
    + '<div class="boat-dropdown" id="picker-dropdown" style="display:none"></div>'
    + '</div></div>';

  body.innerHTML = html;
}

async function openPicker() {
  const r = await fetch('/api/boats?exclude_race=' + SESSION_ID);
  _pickerBoats = await r.json();
  showBoatDropdown('');
  document.getElementById('picker-dropdown').style.display = '';
}

function closePicker() {
  setTimeout(() => {
    const dd = document.getElementById('picker-dropdown');
    if (dd) dd.style.display = 'none';
  }, 200);
}

function filterBoats(text) {
  if (_pickerBoats) {
    showBoatDropdown(text);
    document.getElementById('picker-dropdown').style.display = '';
  }
}

function showBoatDropdown(searchText) {
  const q = searchText.trim().toLowerCase();
  const filtered = q
    ? _pickerBoats.filter(b => b.sail_number.toLowerCase().includes(q) || (b.name || '').toLowerCase().includes(q))
    : _pickerBoats;
  let html = filtered.slice(0, 15).map(b => {
    const label = esc(b.name ? b.sail_number + ' — ' + b.name : b.sail_number);
    return '<div class="boat-option" onmousedown="event.preventDefault()" onclick="selectBoat(' + b.id + ')">' + label + '</div>';
  }).join('');
  const exactMatch = filtered.some(b => b.sail_number.toLowerCase() === searchText.trim().toLowerCase());
  if (searchText.trim() && !exactMatch) {
    const js = searchText.trim().replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    html += '<div class="boat-option boat-option-new" onmousedown="event.preventDefault()" onclick="selectNewBoat(\'' + js + '\')">+ Add &ldquo;' + esc(searchText.trim()) + '&rdquo;</div>';
  }
  if (!html) html = '<div class="boat-option" style="color:var(--text-secondary);cursor:default">No boats found</div>';
  document.getElementById('picker-dropdown').innerHTML = html;
}

async function selectBoat(boatId) {
  const list = document.getElementById('results-list');
  const nextPlace = list ? list.children.length + 1 : 1;
  await fetch('/api/sessions/' + SESSION_ID + '/results', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({place: nextPlace, boat_id: boatId})
  });
  _pickerBoats = null;
  loadResults();
}

async function selectNewBoat(sailNumber) {
  const list = document.getElementById('results-list');
  const nextPlace = list ? list.children.length + 1 : 1;
  await fetch('/api/sessions/' + SESSION_ID + '/results', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({place: nextPlace, sail_number: sailNumber})
  });
  _pickerBoats = null;
  loadResults();
}

async function toggleFlag(place, boatId, dnf, dns) {
  await fetch('/api/sessions/' + SESSION_ID + '/results', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({place, boat_id: boatId, dnf, dns})
  });
  loadResults();
}

async function deleteResult(resultId) {
  await fetch('/api/results/' + resultId, {method: 'DELETE'});
  loadResults();
}

// ---------------------------------------------------------------------------
// Crew
// ---------------------------------------------------------------------------

let _sessionCrew = [];
let _sessionCrewUsers = [];
let _sessionCrewPositions = [];
const _sessionUserRole = cfg.dataset.userRole || 'viewer';

async function loadCrew() {
  const card = document.getElementById('crew-card');
  card.style.display = '';
  const body = document.getElementById('crew-body');
  const r = await fetch('/api/races/' + SESSION_ID + '/crew');
  const data = await r.json();
  _sessionCrew = data.crew || [];
  renderCrewCollapsed();
  // Start collapsed — show summary
  _collapsed['crew'] = true;
  body.style.display = 'none';
  document.getElementById('crew-toggle').innerHTML = '&#9654;';
}

function renderCrewCollapsed() {
  const summary = document.getElementById('crew-summary');
  if (!summary) return;
  if (_sessionCrew.length) {
    let totalBody = 0, totalGear = 0, hasWeight = false;
    const lines = _sessionCrew.map(c => {
      const pos = esc(c.position.charAt(0).toUpperCase() + c.position.slice(1));
      const name = c.attributed ? esc(c.user_name || '\u2014') : '<em>(not attributed)</em>';
      let wt = '';
      if (c.body_weight != null || c.gear_weight != null) {
        hasWeight = true;
        const b = c.body_weight || 0;
        const g = c.gear_weight || 0;
        totalBody += b;
        totalGear += g;
        wt = ' <span style="color:var(--text-muted);font-size:.75rem">('
          + (b ? b.toFixed(0) : '0');
        if (g) wt += '+' + g.toFixed(0) + 'g';
        wt += ' lbs)</span>';
      }
      return '<span style="color:var(--text-secondary)">' + pos + ':</span> ' + name + wt;
    });
    let html = lines.join(' &nbsp;\u00b7&nbsp; ');
    if (hasWeight) {
      const total = totalBody + totalGear;
      html += '<div style="color:var(--text-secondary);font-size:.78rem;margin-top:4px">'
        + 'Total crew weight: ' + total.toFixed(0) + ' lbs'
        + ' (body ' + totalBody.toFixed(0) + ' + gear ' + totalGear.toFixed(0) + ')</div>';
    }
    summary.innerHTML = html;
  } else {
    summary.innerHTML = '<span style="color:var(--text-secondary)">No crew recorded</span>';
  }
}

async function loadCrewEditForm() {
  const body = document.getElementById('crew-body');
  if (!body) return;
  // Lazy-load positions and users
  if (!_sessionCrewPositions.length) {
    const [posR, usrR] = await Promise.all([
      fetch('/api/crew/positions'),
      fetch('/api/crew/users'),
    ]);
    _sessionCrewPositions = (await posR.json()).positions || [];
    _sessionCrewUsers = (await usrR.json()).users || [];
  }
  const canEdit = _sessionUserRole === 'admin' || _sessionUserRole === 'crew';
  let html = '';
  for (const p of _sessionCrewPositions) {
    const label = esc(p.name.charAt(0).toUpperCase() + p.name.slice(1));
    const entry = _sessionCrew.find(c => c.position_id === p.id);
    const curVal = entry && entry.user_id ? String(entry.user_id) : '';
    const bodyWt = entry && entry.body_weight != null ? entry.body_weight : '';
    const gearWt = entry && entry.gear_weight != null ? entry.gear_weight : '';
    html += '<div class="crew-row" data-pos-id="' + p.id + '">';
    html += '<span class="crew-pos">' + label + '</span>';
    html += '<select class="crew-select' + (curVal ? ' has-value' : '') + '" '
      + (canEdit ? 'onchange="onSessionCrewChange(this)"' : 'disabled') + '>';
    html += '<option value="">\u2014</option>';
    // Build filtered options
    const taken = new Set();
    for (const c of _sessionCrew) {
      if (c.user_id && c.position_id !== p.id) taken.add(String(c.user_id));
    }
    for (const u of _sessionCrewUsers) {
      const uid = String(u.id);
      if (uid === curVal || !taken.has(uid)) {
        const n = esc(u.name || u.email);
        const suffix = u.pending ? ' (invited)' : '';
        html += '<option value="' + uid + '"' + (uid === curVal ? ' selected' : '') + '>' + n + suffix + '</option>';
      }
    }
    if (canEdit) html += '<option value="__new__">+ Add new...</option>';
    html += '</select>';
    html += '<input type="number" class="crew-weight" data-field="body" step="0.1" min="0" max="500"'
      + ' placeholder="Body lbs" title="Body weight (lbs)" value="' + bodyWt + '"'
      + (canEdit ? ' onchange="onSessionCrewWeightChange()"' : ' disabled') + '/>';
    html += '<input type="number" class="crew-weight" data-field="gear" step="0.1" min="0" max="100"'
      + ' placeholder="Gear lbs" title="Gear weight (lbs)" value="' + gearWt + '"'
      + (canEdit ? ' onchange="onSessionCrewWeightChange()"' : ' disabled') + '/>';
    html += '</div>';
  }
  html += '<div id="session-crew-total" class="crew-total-weight"></div>';
  body.innerHTML = html;
  _updateSessionCrewTotal();
}

function toggleCrewSection() {
  _collapsed['crew'] = !_collapsed['crew'];
  const body = document.getElementById('crew-body');
  const toggle = document.getElementById('crew-toggle');
  const summary = document.getElementById('crew-summary');
  if (_collapsed['crew']) {
    body.style.display = 'none';
    summary.style.display = '';
    toggle.innerHTML = '&#9654;';
  } else {
    body.style.display = '';
    summary.style.display = 'none';
    toggle.innerHTML = '&#9660;';
    loadCrewEditForm();
  }
}

async function onSessionCrewChange(selectEl) {
  // Handle "Add new..."
  if (selectEl && selectEl.value === '__new__') {
    selectEl.value = '';
    const name = prompt('New crew member name:');
    if (name && name.trim()) {
      try {
        const r = await fetch('/api/crew/placeholder', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: name.trim()})
        });
        if (r.ok) {
          const data = await r.json();
          _sessionCrewUsers.push({id: data.id, name: data.name, email: '', role: 'viewer'});
          const opt = document.createElement('option');
          opt.value = String(data.id);
          selectEl.appendChild(opt);
          selectEl.value = String(data.id);
        }
      } catch (e) { console.error('create placeholder error', e); }
    }
  }
  // Auto-default body weight from user profile when user is selected
  if (selectEl) {
    const row = selectEl.closest('.crew-row');
    const bodyInput = row.querySelector('.crew-weight[data-field="body"]');
    const uid = selectEl.value ? parseInt(selectEl.value) : null;
    if (uid) {
      const user = _sessionCrewUsers.find(u => u.id === uid);
      if (user && user.weight_lbs != null && !bodyInput.value) {
        bodyInput.value = user.weight_lbs;
      }
    } else {
      bodyInput.value = '';
      row.querySelector('.crew-weight[data-field="gear"]').value = '';
    }
  }
  await _saveSessionCrew();
}

function onSessionCrewWeightChange() {
  _updateSessionCrewTotal();
  _saveSessionCrew();
}

function _updateSessionCrewTotal() {
  const el = document.getElementById('session-crew-total');
  if (!el) return;
  let totalBody = 0, totalGear = 0, count = 0;
  document.querySelectorAll('#crew-body .crew-row').forEach(row => {
    const bv = parseFloat(row.querySelector('.crew-weight[data-field="body"]').value);
    const gv = parseFloat(row.querySelector('.crew-weight[data-field="gear"]').value);
    if (!isNaN(bv)) { totalBody += bv; count++; }
    if (!isNaN(gv)) totalGear += gv;
  });
  const total = totalBody + totalGear;
  if (count > 0) {
    el.innerHTML = '<strong>Total weight: ' + total.toFixed(1) + ' lbs</strong>'
      + ' <span style="color:var(--text-secondary)">=&nbsp;crew ' + totalBody.toFixed(1)
      + '&nbsp;+&nbsp;gear ' + totalGear.toFixed(1) + '</span>';
    el.style.display = 'block';
  } else {
    el.style.display = 'none';
  }
}

async function _saveSessionCrew() {
  // Collect crew with weights
  const crew = [];
  document.querySelectorAll('#crew-body .crew-row').forEach(row => {
    const posId = parseInt(row.dataset.posId);
    const sel = row.querySelector('.crew-select');
    const userId = sel.value && sel.value !== '__new__' ? parseInt(sel.value) : null;
    const bodyVal = row.querySelector('.crew-weight[data-field="body"]').value;
    const gearVal = row.querySelector('.crew-weight[data-field="gear"]').value;
    const bodyWeight = bodyVal ? parseFloat(bodyVal) : null;
    const gearWeight = gearVal ? parseFloat(gearVal) : null;
    if (userId || bodyWeight != null || gearWeight != null) {
      crew.push({position_id: posId, user_id: userId, body_weight: bodyWeight, gear_weight: gearWeight});
    }
    sel.classList.toggle('has-value', !!sel.value && sel.value !== '__new__');
  });
  await fetch('/api/races/' + SESSION_ID + '/crew', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(crew)
  });
  // Reload resolved crew and update summary
  const r = await fetch('/api/races/' + SESSION_ID + '/crew');
  const data = await r.json();
  _sessionCrew = data.crew || [];
  renderCrewCollapsed();
  // Refresh dropdowns to filter assigned users
  loadCrewEditForm();
}

// ---------------------------------------------------------------------------
// Sails
// ---------------------------------------------------------------------------

async function loadSails() {
  const card = document.getElementById('sails-card');
  card.style.display = '';
  const body = document.getElementById('sails-body');
  const [sailsResp, inventoryResp] = await Promise.all([
    fetch('/api/sessions/' + SESSION_ID + '/sails'),
    fetch('/api/sails'),
  ]);
  const current = await sailsResp.json();
  const inventory = await inventoryResp.json();
  const slots = ['main', 'jib', 'spinnaker'];
  let html = '';
  slots.forEach(slot => {
    const opts = (inventory[slot] || []).map(s =>
      '<option value="' + s.id + '"' + (current[slot] && current[slot].id === s.id ? ' selected' : '') + '>'
      + esc(s.name) + '</option>'
    ).join('');
    html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">'
      + '<span style="color:var(--text-secondary);width:68px;flex-shrink:0">' + slot.charAt(0).toUpperCase() + slot.slice(1) + '</span>'
      + '<select id="sail-select-' + slot + '" style="flex:1;background:var(--bg-secondary);color:var(--text-primary);border:1px solid var(--accent-strong);border-radius:4px;padding:3px 6px;font-size:.78rem">'
      + '<option value="">\u2014 none \u2014</option>' + opts
      + '</select></div>';
  });
  html += '<button class="btn-export" style="background:var(--accent-strong);color:var(--bg-primary);border-color:var(--accent-strong);font-size:.78rem;margin-top:4px" onclick="saveSails()">Save Sails</button>';
  html += '<div id="sail-changes-timeline"></div>';
  body.innerHTML = html;

  // If no sails are set for this session, pre-select from boat-level defaults
  const hasAnySail = slots.some(slot => current[slot] && current[slot].id);
  if (!hasAnySail) {
    try {
      const defaultsResp = await fetch('/api/sails/defaults');
      if (defaultsResp.ok) {
        const defaults = await defaultsResp.json();
        slots.forEach(slot => {
          if (defaults[slot] && defaults[slot].id) {
            const sel = document.getElementById('sail-select-' + slot);
            if (sel) sel.value = String(defaults[slot].id);
          }
        });
      }
    } catch (_) { /* ignore — defaults are a convenience, not critical */ }
  }
  await loadSailChangeTimeline();
}

async function saveSails() {
  const slots = ['main', 'jib', 'spinnaker'];
  const payload = {};
  slots.forEach(slot => {
    const sel = document.getElementById('sail-select-' + slot);
    payload[slot + '_id'] = sel && sel.value ? parseInt(sel.value, 10) : null;
  });
  const r = await fetch('/api/sessions/' + SESSION_ID + '/sails', {
    method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
  });
  if (!r.ok) { alert('Failed to save sails'); return; }
  await loadSailChangeTimeline();
}

async function loadSailChangeTimeline() {
  const container = document.getElementById('sail-changes-timeline');
  if (!container) return;
  try {
    const r = await fetch('/api/sessions/' + SESSION_ID + '/sail-changes');
    if (!r.ok) return;
    const data = await r.json();
    const changes = data.changes || [];
    if (changes.length <= 1) {
      container.style.display = 'none';
      return;
    }
    container.style.display = 'block';
    let html = '<div style="font-size:.75rem;color:var(--text-secondary);margin-top:8px;border-top:1px solid ' + cssVar('--border') + ';padding-top:8px">'
      + '<strong>Sail Changes</strong></div>';
    html += '<div style="font-size:.75rem;margin-top:4px">';
    changes.forEach((c, i) => {
      const ts = c.ts ? new Date(c.ts + (c.ts.endsWith('Z') ? '' : 'Z')) : null;
      const timeStr = ts ? ts.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'}) : '—';
      const names = ['main', 'jib', 'spinnaker']
        .filter(s => c[s] && c[s].name)
        .map(s => esc(c[s].name));
      const label = names.length ? names.join(' · ') : '(none)';
      const isFirst = i === 0;
      html += '<div style="display:flex;gap:8px;align-items:baseline;margin-bottom:2px">'
        + '<span style="color:var(--text-secondary);min-width:70px">' + timeStr + '</span>'
        + '<span' + (isFirst ? ' style="color:var(--text-muted)"' : '') + '>'
        + (isFirst ? '(start) ' : '') + label + '</span>'
        + '</div>';
    });
    html += '</div>';
    container.innerHTML = html;
  } catch (e) { console.error('sail changes timeline error', e); }
}

// ---------------------------------------------------------------------------
// Notes
// ---------------------------------------------------------------------------

async function loadNotes() {
  const card = document.getElementById('notes-card');
  card.style.display = '';
  const body = document.getElementById('notes-body');
  const r = await fetch('/api/sessions/' + SESSION_ID + '/notes');
  const notes = await r.json();
  if (notes.length) {
    body.innerHTML = notes.map(n => {
      const t = fmtTime(n.ts);
      let content = '';
      if (n.note_type === 'photo' && n.photo_path) {
        const src = '/notes/' + n.photo_path;
        content = '<img src="' + src + '" loading="lazy" style="max-width:100px;max-height:80px;border-radius:4px;cursor:pointer;margin-top:2px" onclick="window.open(this.dataset.src)" data-src="' + src + '"/>';
      } else if (n.note_type === 'settings' && n.body) {
        try {
          const obj = JSON.parse(n.body);
          content = Object.entries(obj).map(([k, v]) =>
            '<span style="color:var(--text-secondary)">' + esc(k) + ':</span> ' + esc(v)
          ).join(' &middot; ');
        } catch { content = esc(n.body); }
      } else {
        content = esc(n.body);
      }
      const del = '<button onclick="deleteNote(' + n.id + ')" style="background:none;border:none;color:var(--danger);cursor:pointer;font-size:.8rem;padding:0 4px;float:right">&#10005;</button>';
      return '<div style="padding:4px 0;border-bottom:1px solid ' + cssVar('--border') + ';overflow:hidden">'
        + del + '<span style="color:var(--text-secondary);margin-right:6px">' + t + '</span>' + content + '</div>';
    }).join('');
  } else {
    body.innerHTML = '<span style="color:var(--text-secondary)">No notes</span>';
  }
}

async function deleteNote(noteId) {
  await fetch('/api/notes/' + noteId, {method: 'DELETE'});
  loadNotes();
}

// ---------------------------------------------------------------------------
// Transcript
// ---------------------------------------------------------------------------

async function loadTranscript() {
  const card = document.getElementById('transcript-card');
  card.style.display = '';
  const body = document.getElementById('transcript-body');
  body.innerHTML = '<span style="color:var(--text-secondary)">Loading\u2026</span>';

  const r = await fetch('/api/audio/' + _session.audio_session_id + '/transcript');
  if (r.status === 404) {
    body.innerHTML = '<span style="color:var(--text-secondary)">No transcript yet. </span>'
      + '<button class="btn-export" style="font-size:.75rem" onclick="startTranscript()">&#9654; Transcribe</button>';
    return;
  }
  const t = await r.json();
  if (t.status === 'pending' || t.status === 'running') {
    body.innerHTML = '<span style="color:var(--warning)">Transcription in progress\u2026</span>';
    setTimeout(loadTranscript, 3000);
    return;
  }
  if (t.status === 'error') {
    body.innerHTML = '<span style="color:var(--danger)">Error: ' + esc(t.error_msg || 'unknown') + '</span>';
    return;
  }
  // Store transcript ID for tuning extraction
  if (t.id) {
    _transcriptId = t.id;
    loadTuningExtractions();
  }
  _speakerMap = t.speaker_map || {};
  // Check if segments have speaker labels (diarized) vs plain whisper segments
  const hasDiarizedSegments = t.segments && t.segments.length > 0
    && t.segments.some(s => s.speaker);
  if (hasDiarizedSegments) {
    _renderDiarizedTranscript(body, t);
  } else {
    const text = t.text ? esc(t.text) : '(empty)';
    body.innerHTML = '<div style="font-size:.8rem;color:var(--text-primary);white-space:pre-wrap;max-height:300px;overflow-y:auto;background:var(--bg-secondary);border-radius:6px;padding:8px">' + text + '</div>'
      + '<div style="margin-top:8px"><button class="btn-export" style="font-size:.75rem" onclick="retranscribe()" title="Re-run transcription with speaker diarization">&#8635; Retranscribe with diarization</button></div>';
  }
}

function _renderDiarizedTranscript(body, t) {
  const blocks = [];
  for (const seg of t.segments) {
    const last = blocks[blocks.length - 1];
    if (last && last.speaker === seg.speaker) {
      last.text += ' ' + seg.text; last.end = seg.end;
    } else { blocks.push({...seg}); }
  }
  _transcriptBlocks = blocks;
  const rawSpeakers = [...new Set(t.segments.map(s => s.speaker))];
  const speakers = [...new Set(blocks.map(b => b.speaker))];
  const palette = [cssVar('--accent'), cssVar('--success'), cssVar('--warning'), cssVar('--danger'), '#c4b5fd', '#f9a8d4'];
  const color = s => palette[rawSpeakers.indexOf(s) >= 0 ? rawSpeakers.indexOf(s) % palette.length : speakers.indexOf(s) % palette.length];
  const fmt = s => { const m = Math.floor(s / 60); return m + ':' + String(Math.floor(s % 60)).padStart(2, '0'); };

  // Display name for a speaker (crew name from speaker_map, or raw label)
  const displayName = (rawLabel) => {
    const entry = _speakerMap[rawLabel];
    if (entry && entry.name) {
      if (entry.type === 'auto' && entry.confidence != null) {
        return entry.name + ' (' + Math.round(entry.confidence * 100) + '%)';
      }
      return entry.name;
    }
    return rawLabel;
  };

  body.innerHTML = '<div id="transcript-segments" style="max-height:400px;overflow-y:auto;background:var(--bg-secondary);border-radius:6px;padding:8px">'
    + blocks.map((b, i) =>
      '<div class="transcript-seg" data-idx="' + i + '" style="margin-bottom:8px;padding:4px 6px;border-radius:4px;cursor:pointer;transition:background .15s" onclick="playTranscriptSegment(' + i + ')">'
      + '<span class="transcript-speaker" data-speaker="' + esc(b.speaker) + '" style="color:' + color(b.speaker) + ';font-weight:600;font-size:.75rem;cursor:pointer;text-decoration:underline dotted;text-underline-offset:2px" onclick="event.stopPropagation();openSpeakerPicker(\'' + esc(b.speaker) + '\')" title="Click to assign crew">' + esc(displayName(b.speaker)) + '</span>'
      + '<span style="color:var(--text-secondary);font-size:.7rem;margin-left:4px">[' + fmt(b.start) + ']</span>'
      + '<div style="color:var(--text-primary);font-size:.8rem;margin-top:2px">' + esc(b.text.trim()) + '</div>'
      + '</div>'
    ).join('')
    + '</div>';

  // Register the diarized transcript as a playback-clock surface so it
  // highlights the active segment in sync with audio/video/map (#446).
  _registerTranscriptSurface();
}

let _transcriptSurfaceRegistered = false;
function _registerTranscriptSurface() {
  if (!_session || !_session.audio_start_utc) return;
  if (_transcriptSurfaceRegistered) return; // idempotent — transcript may re-render on poll
  _transcriptSurfaceRegistered = true;
  const audioStart = new Date(
    _session.audio_start_utc.endsWith('Z') || _session.audio_start_utc.includes('+')
      ? _session.audio_start_utc
      : _session.audio_start_utc + 'Z'
  );
  registerSurface('transcript', function(utc) {
    if (!_transcriptBlocks.length) return;
    const local = (utc.getTime() - audioStart.getTime()) / 1000;
    const segs = document.querySelectorAll('.transcript-seg');
    for (let i = 0; i < _transcriptBlocks.length; i++) {
      const b = _transcriptBlocks[i];
      const el = segs[i];
      if (!el) continue;
      if (local >= b.start && local <= b.end) {
        el.style.background = 'var(--bg-hover, rgba(255,255,255,0.08))';
        const container = document.getElementById('transcript-segments');
        if (container && (el.offsetTop < container.scrollTop || el.offsetTop + el.offsetHeight > container.scrollTop + container.clientHeight)) {
          el.scrollIntoView({behavior: 'smooth', block: 'nearest'});
        }
      } else {
        el.style.background = '';
      }
    }
  });
}

function playTranscriptSegment(idx) {
  const b = _transcriptBlocks[idx];
  if (!b) return;
  // Route through the playback clock so video and map follow too.
  if (_session && _session.audio_start_utc) {
    const audioStart = new Date(
      _session.audio_start_utc.endsWith('Z') || _session.audio_start_utc.includes('+')
        ? _session.audio_start_utc
        : _session.audio_start_utc + 'Z'
    );
    setPosition(new Date(audioStart.getTime() + b.start * 1000), {source: 'transcript'});
  }
  const audioEl = document.getElementById('session-audio')
    || document.querySelector('#audio-body audio');
  if (audioEl) {
    _transcriptAudio = audioEl;
    audioEl.currentTime = b.start;
    audioEl.play();
  }
}

async function openSpeakerPicker(speakerLabel) {
  // Fetch crew list for the picker
  let users;
  try {
    const r = await fetch('/api/crew/users');
    if (!r.ok) return;
    users = (await r.json()).users || [];
  } catch { return; }

  // Remove any existing picker
  const old = document.getElementById('speaker-picker');
  if (old) old.remove();

  // Build a simple dropdown picker
  const picker = document.createElement('div');
  picker.id = 'speaker-picker';
  picker.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--bg-primary);border:1px solid var(--border);border-radius:8px;padding:16px;z-index:1000;box-shadow:0 4px 20px rgba(0,0,0,0.3);min-width:200px;max-height:300px;overflow-y:auto';

  let html = '<div style="font-weight:600;margin-bottom:8px;font-size:.85rem">Assign ' + esc(speakerLabel) + ' to:</div>';
  if (!users || !users.length) {
    html += '<div style="color:var(--text-secondary);font-size:.8rem">No crew members found</div>';
  } else {
    for (const u of users) {
      html += '<div class="speaker-pick-option" style="padding:6px 8px;cursor:pointer;border-radius:4px;font-size:.8rem" onmouseover="this.style.background=\'var(--bg-hover, rgba(255,255,255,0.08))\'" onmouseout="this.style.background=\'\'" onclick="assignSpeaker(\'' + esc(speakerLabel) + '\',' + u.id + ')">' + esc(u.name || u.email) + '</div>';
    }
  }
  html += '<div style="text-align:right;margin-top:8px"><button class="btn-export" style="font-size:.75rem" onclick="document.getElementById(\'speaker-picker\').remove()">Cancel</button></div>';
  picker.innerHTML = html;

  // Backdrop
  const backdrop = document.createElement('div');
  backdrop.id = 'speaker-picker-backdrop';
  backdrop.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.4);z-index:999';
  backdrop.onclick = () => { picker.remove(); backdrop.remove(); };
  document.body.appendChild(backdrop);
  document.body.appendChild(picker);
}

async function assignSpeaker(speakerLabel, userId) {
  const r = await fetch('/api/audio/' + _session.audio_session_id + '/transcript/assign-speaker', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({speaker_label: speakerLabel, user_id: userId})
  });
  // Clean up picker
  const picker = document.getElementById('speaker-picker');
  const backdrop = document.getElementById('speaker-picker-backdrop');
  if (picker) picker.remove();
  if (backdrop) backdrop.remove();

  if (!r.ok) { alert('Failed to assign speaker'); return; }
  const data = await r.json();
  // Update speaker_map locally and re-render labels
  _speakerMap[speakerLabel] = {type: 'crew', user_id: data.user_id, name: data.name};
  // Update all speaker labels in the transcript
  document.querySelectorAll('.transcript-speaker[data-speaker="' + speakerLabel + '"]').forEach(el => {
    el.textContent = data.name;
  });
}

async function startTranscript() {
  const r = await fetch('/api/audio/' + _session.audio_session_id + '/transcribe', {method: 'POST'});
  if (!r.ok) { alert('Failed to start transcription'); return; }
  loadTranscript();
}

async function retranscribe() {
  if (!confirm('Re-run transcription with diarization? The existing transcript will be replaced.')) return;
  const r = await fetch('/api/audio/' + _session.audio_session_id + '/retranscribe', {method: 'POST'});
  if (!r.ok) { alert('Failed to start retranscription'); return; }
  loadTranscript();
}

// ---------------------------------------------------------------------------
// Audio
// ---------------------------------------------------------------------------

function loadAudio() {
  const card = document.getElementById('audio-card');
  card.style.display = '';
  document.getElementById('audio-body').innerHTML =
    '<audio id="session-audio" controls style="width:100%">'
    + '<source src="/api/audio/' + _session.audio_session_id + '/stream" type="audio/wav">'
    + '</audio>';
  const el = document.getElementById('session-audio');
  if (!el) return;
  // Always wire audio→transcript highlighting (works even if audio_start_utc
  // is missing — segments use audio-local seconds, same as el.currentTime).
  el.addEventListener('timeupdate', _highlightTranscriptFromAudio);
  el.addEventListener('seeked', _highlightTranscriptFromAudio);

  if (!_session.audio_start_utc) return;
  const audioStart = new Date(
    _session.audio_start_utc.endsWith('Z') || _session.audio_start_utc.includes('+')
      ? _session.audio_start_utc
      : _session.audio_start_utc + 'Z'
  );

  const audioLocalToUtc = s => new Date(audioStart.getTime() + s * 1000);
  const utcToAudioLocal = utc => (utc.getTime() - audioStart.getTime()) / 1000;

  // Audio is a consumer — seek to the requested UTC if it's within range
  registerSurface('audio', function(utc) {
    const local = utcToAudioLocal(utc);
    if (local < 0 || (el.duration && local > el.duration)) return;
    if (Math.abs(el.currentTime - local) < 0.15) return; // already there
    try { el.currentTime = local; } catch (e) { /* not seekable yet */ }
  });

  // Audio is a producer — fan out to other surfaces when user scrubs/plays.
  // timeupdate fires ~4 Hz during playback, so this drives the map cursor and
  // any other UTC-based consumers in real time.
  let _audioFanoutLast = 0;
  const _fanout = () => {
    if (_isEchoEvent()) return;
    const now = _clockNowMs();
    if (now - _audioFanoutLast < 150) return; // throttle
    _audioFanoutLast = now;
    setPosition(audioLocalToUtc(el.currentTime), {source: 'audio'});
  };
  el.addEventListener('seeked', _fanout);
  el.addEventListener('timeupdate', _fanout);
  el.addEventListener('play', function() {
    setPosition(audioLocalToUtc(el.currentTime), {source: 'audio'});
    _startPlayTick();
  });
  el.addEventListener('pause', function() {
    _stopPlayTick();
  });
}

// Direct transcript highlighter — follows audio.currentTime regardless of
// playback-clock state. Drives the same active-segment styling and scroll
// behavior as the clock-driven path.
function _highlightTranscriptFromAudio(ev) {
  const el = ev && ev.target ? ev.target : document.getElementById('session-audio');
  if (!el || !_transcriptBlocks.length) return;
  const t = el.currentTime;
  const segs = document.querySelectorAll('.transcript-seg');
  let activeIdx = -1;
  for (let i = 0; i < _transcriptBlocks.length; i++) {
    const b = _transcriptBlocks[i];
    if (t >= b.start && t <= b.end) { activeIdx = i; break; }
  }
  for (let i = 0; i < segs.length; i++) {
    const segEl = segs[i];
    if (!segEl) continue;
    if (i === activeIdx) {
      segEl.style.background = 'var(--bg-hover, rgba(255,255,255,0.08))';
      const container = document.getElementById('transcript-segments');
      if (container && (segEl.offsetTop < container.scrollTop
          || segEl.offsetTop + segEl.offsetHeight > container.scrollTop + container.clientHeight)) {
        segEl.scrollIntoView({behavior: 'smooth', block: 'nearest'});
      }
    } else {
      segEl.style.background = '';
    }
  }
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Co-op Sharing
// ---------------------------------------------------------------------------

async function loadSharing() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/sharing');
  if (!r.ok) return;
  const data = await r.json();
  if (!data.co_ops || !data.co_ops.length) return;

  const card = document.getElementById('sharing-card');
  card.style.display = '';
  renderSharing(data);
}

function renderSharing(data) {
  const body = document.getElementById('sharing-body');
  let html = '<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">';
  for (const c of data.co_ops) {
    if (c.shared) {
      html += '<button class="btn-export" style="background:var(--bg-secondary);border:1px solid var(--success);color:var(--success)"'
        + ' onclick="unshareSession(\'' + esc(c.co_op_id) + '\')">'
        + esc(c.co_op_name) + ' &#10003;</button>';
    } else {
      html += '<button class="btn-export" style="background:var(--bg-secondary);border:1px solid var(--border);color:var(--text-primary)"'
        + ' onclick="shareSession(\'' + esc(c.co_op_id) + '\')">'
        + 'Share with ' + esc(c.co_op_name) + '</button>';
    }
  }
  html += '</div>';

  // Show sharing details
  if (data.sharing && data.sharing.length) {
    html += '<div style="margin-top:8px;font-size:.78rem;color:var(--text-secondary)">';
    for (const s of data.sharing) {
      html += '<div>Shared with <strong style="color:var(--text-primary)">' + esc(s.co_op_name || s.co_op_id) + '</strong>';
      if (s.embargo_until) html += ' (embargo until ' + esc(s.embargo_until).slice(0, 10) + ')';
      html += ' &mdash; ' + esc(s.shared_at).slice(0, 19) + '</div>';
    }
    html += '</div>';
  }
  body.innerHTML = html;
}

async function shareSession(coopId) {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/share', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({co_op_id: coopId})
  });
  if (r.ok) { loadSharing(); } else {
    const d = await r.json().catch(() => ({}));
    alert(d.detail || 'Failed to share');
  }
}

async function unshareSession(coopId) {
  if (!confirm('Stop sharing this session with this co-op?')) return;
  const r = await fetch('/api/sessions/' + SESSION_ID + '/share/' + encodeURIComponent(coopId), {
    method: 'DELETE'
  });
  if (r.ok) { loadSharing(); } else {
    const d = await r.json().catch(() => ({}));
    alert(d.detail || 'Failed to unshare');
  }
}

// ---------------------------------------------------------------------------
// Session Match (#281)
// ---------------------------------------------------------------------------

async function loadMatch() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/match');
  if (!r.ok) return;
  const data = await r.json();

  const card = document.getElementById('match-card');
  const body = document.getElementById('match-body');
  const role = document.getElementById('app-config').dataset.userRole;
  const isAdmin = role === 'admin';

  if (data.status === 'unmatched') {
    if (!isAdmin) return; // viewers don't see scan button
    card.style.display = '';
    body.innerHTML = '<div style="font-size:.82rem;color:var(--text-secondary)">No match found.</div>'
      + '<button class="btn-export" style="margin-top:6px" onclick="scanForMatches()">Scan for matches</button>';
    return;
  }

  card.style.display = '';
  let html = '<div style="font-size:.82rem">';

  // Peer info line (shared between candidate and confirmed states)
  const peerLine = data.peer_boat_name
    ? '<div style="color:var(--text-secondary);margin-bottom:6px">Matched boat: <strong style="color:var(--text-primary)">'
      + esc(data.peer_boat_name) + '</strong>'
      + (data.peer_session_name ? ' — ' + esc(data.peer_session_name) : '')
      + '</div>'
    : '';

  if (data.status === 'candidate') {
    html += '<div style="color:var(--warning);margin-bottom:6px">Pending match — awaiting confirmation</div>';
    html += peerLine;
    if (isAdmin) {
      html += '<div style="display:flex;gap:6px">'
        + '<button class="btn-export" style="background:var(--bg-secondary);border:1px solid var(--success);color:var(--success)" onclick="confirmMatch()">Confirm</button>'
        + '<button class="btn-export" style="background:var(--bg-secondary);border:1px solid var(--danger);color:var(--danger)" onclick="rejectMatch()">Reject</button>'
        + '</div>';
    }
  } else if (data.status === 'confirmed') {
    html += '<div style="color:var(--success);margin-bottom:6px">Matched with co-op boats</div>';
    html += peerLine;
    if (data.shared_name) {
      html += '<div style="margin-bottom:4px">Shared name: <strong style="color:var(--text-primary)">' + esc(data.shared_name) + '</strong></div>';
    }
    if (isAdmin) {
      html += '<div style="margin-top:6px">'
        + '<input type="text" id="match-name-input" value="' + esc(data.shared_name || '') + '"'
        + ' placeholder="Set shared name" style="background:var(--bg-input);border:1px solid ' + cssVar('--border') + ';border-radius:4px;color:var(--text-primary);padding:4px 8px;font-size:.8rem;width:60%">'
        + ' <button class="btn-export" onclick="setMatchName()">Save</button>'
        + '</div>';
    }
  }

  html += '</div>';
  body.innerHTML = html;
}

async function scanForMatches() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/match/scan', {method: 'POST'});
  if (r.ok) {
    const d = await r.json();
    const n = d.proposals ? d.proposals.length : 0;
    alert(n + ' match proposal(s) sent to peers.');
    loadMatch();
    renderHeader();
  } else {
    const d = await r.json().catch(() => ({}));
    alert(d.detail || 'Scan failed');
  }
}

async function confirmMatch() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/match/confirm', {method: 'POST'});
  if (r.ok) {
    loadMatch();
    // Reload detail to update header badges
    const dr = await fetch('/api/sessions/' + SESSION_ID + '/detail');
    if (dr.ok) { _session = await dr.json(); renderHeader(); }
  } else {
    const d = await r.json().catch(() => ({}));
    alert(d.detail || 'Confirm failed');
  }
}

async function rejectMatch() {
  if (!confirm('Reject this session match?')) return;
  const r = await fetch('/api/sessions/' + SESSION_ID + '/match/reject', {method: 'POST'});
  if (r.ok) {
    loadMatch();
    const dr = await fetch('/api/sessions/' + SESSION_ID + '/detail');
    if (dr.ok) { _session = await dr.json(); renderHeader(); }
  } else {
    const d = await r.json().catch(() => ({}));
    alert(d.detail || 'Reject failed');
  }
}

async function setMatchName() {
  const input = document.getElementById('match-name-input');
  const btn = document.querySelector('#match-body button[onclick="setMatchName()"]');
  const name = input ? input.value.trim() : '';
  if (!name) { alert('Enter a name'); return; }
  if (btn) { btn.textContent = 'Saving...'; btn.disabled = true; }
  const r = await fetch('/api/sessions/' + SESSION_ID + '/match/name', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: name})
  });
  if (r.ok) {
    if (btn) { btn.textContent = 'Saved ✓'; setTimeout(() => { btn.textContent = 'Save'; btn.disabled = false; }, 1500); }
    loadMatch();
    const dr = await fetch('/api/sessions/' + SESSION_ID + '/detail');
    if (dr.ok) { _session = await dr.json(); renderHeader(); }
  } else {
    if (btn) { btn.textContent = 'Save'; btn.disabled = false; }
    const d = await r.json().catch(() => ({}));
    alert(d.detail || 'Failed to set name');
  }
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

function renderExports() {
  const s = _session;
  if (s.type === 'debrief' || !s.end_utc) return;

  const card = document.getElementById('exports-card');
  card.style.display = '';
  const body = document.getElementById('exports-body');

  const from = new Date(s.start_utc).getTime();
  const to = new Date(s.end_utc).getTime();
  let html = '';
  html += '<a class="btn-export" href="/api/races/' + s.id + '/export.csv">&#8595; CSV</a>';
  html += '<a class="btn-export" href="/api/races/' + s.id + '/export.gpx">&#8595; GPX</a>';
  html += '<a class="btn-export btn-grafana" href="' + GRAFANA_BASE + '/d/' + GRAFANA_UID + '/sailing-data?from=' + from + '&to=' + to + '&orgId=1&refresh=" target="_blank">&#128202; Grafana</a>';
  if (s.has_audio && s.audio_session_id) {
    html += '<a class="btn-export" href="/api/audio/' + s.audio_session_id + '/download">&#8595; WAV</a>';
  }
  body.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Analysis — staleness indicator + A/B comparison (#412)
// ---------------------------------------------------------------------------

let _analysisResult = null;

async function loadAnalysis() {
  try {
    const r = await fetch('/api/analysis/results/' + SESSION_ID);
    if (!r.ok) return;
    _analysisResult = await r.json();

    const card = document.getElementById('analysis-card');
    card.style.display = '';
    renderAnalysisResult(_analysisResult, document.getElementById('analysis-body'));
  } catch (e) { /* non-fatal */ }
}

function renderAnalysisResult(result, container) {
  let html = '';
  // Staleness banner
  if (result.stale_reason) {
    html += '<div class="stale-banner">'
      + '<span>Analysis outdated: ' + esc(result.stale_reason.replace(/_/g, ' ')) + '</span>'
      + '<button onclick="rerunAnalysis()">Re-run analysis</button>'
      + '</div>';
  }
  // Label
  const label = result.plugin_name
    ? esc(result.plugin_name) + (result.plugin_version ? ' v' + esc(result.plugin_version) : '')
    : '';
  if (label) {
    html += '<div style="font-size:.75rem;color:var(--text-secondary);margin-bottom:6px">' + label + '</div>';
  }
  // Metrics
  const metrics = result.metrics || [];
  if (metrics.length) {
    for (const m of metrics) {
      html += '<div class="metric">'
        + '<span>' + esc(m.label || m.name) + '</span>'
        + '<span><strong>' + esc(String(m.value)) + '</strong>'
        + (m.unit ? ' <span style="color:var(--text-secondary)">' + esc(m.unit) + '</span>' : '')
        + '</span></div>';
    }
  }
  // Insights
  const insights = result.insights || [];
  if (insights.length) {
    for (const i of insights) {
      const cls = i.severity === 'critical' ? 'insight-critical'
        : i.severity === 'warning' ? 'insight-warning' : '';
      html += '<div class="insight ' + cls + '">' + esc(i.message) + '</div>';
    }
  }
  if (!metrics.length && !insights.length) {
    html += '<div style="font-size:.82rem;color:var(--text-secondary)">No analysis data available</div>';
  }
  container.innerHTML = html;
}

async function rerunAnalysis() {
  const body = document.getElementById('analysis-body');
  body.innerHTML = '<div style="font-size:.82rem;color:var(--text-secondary)">Running analysis\u2026</div>';
  try {
    const r = await fetch('/api/analysis/run/' + SESSION_ID, {method: 'POST'});
    if (!r.ok) { body.innerHTML = '<div style="color:var(--danger);font-size:.82rem">Re-run failed</div>'; return; }
    _analysisResult = await r.json();
    renderAnalysisResult(_analysisResult, body);
  } catch (e) {
    body.innerHTML = '<div style="color:var(--danger);font-size:.82rem">Re-run failed: ' + esc(e.message) + '</div>';
  }
}

// A/B Comparison
let _abMode = false;

async function showAbCompare() {
  const body = document.getElementById('analysis-body');
  if (_abMode) { _abMode = false; await loadAnalysis(); return; }
  _abMode = true;

  // Fetch available models
  let plugins;
  try {
    const r = await fetch('/api/analysis/models');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    plugins = await r.json();
  } catch (e) { body.innerHTML = '<div style="color:var(--danger);font-size:.82rem">Failed to load models</div>'; return; }

  if (plugins.length < 2) {
    body.innerHTML = '<div style="font-size:.82rem;color:var(--text-secondary)">At least 2 plugins are needed for A/B comparison</div>';
    return;
  }

  let html = '<div class="ab-selector">';
  for (const p of plugins) {
    html += '<label><input type="checkbox" value="' + esc(p.name) + '" checked> ' + esc(p.display_name) + '</label>';
  }
  html += '<button onclick="runAbCompare()" style="background:var(--accent-strong);color:var(--bg-primary);border:none;border-radius:4px;padding:5px 12px;font-size:.78rem;cursor:pointer">Compare</button>';
  html += '</div><div id="ab-panels"></div>';
  body.innerHTML = html;
}

async function runAbCompare() {
  const checks = document.querySelectorAll('#analysis-body .ab-selector input[type=checkbox]:checked');
  const models = Array.from(checks).map(c => c.value);
  if (models.length < 2) { alert('Select at least 2 models'); return; }
  if (models.length > 5) { alert('Select at most 5 models'); return; }

  const panels = document.getElementById('ab-panels');
  panels.innerHTML = '<div style="font-size:.82rem;color:var(--text-secondary)">Running comparison\u2026</div>';

  try {
    const r = await fetch('/api/analysis/ab-compare/' + SESSION_ID, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({models: models})
    });
    if (!r.ok) { const d = await r.json().catch(() => ({})); panels.innerHTML = '<div style="color:var(--danger);font-size:.82rem">' + esc(d.detail || 'Comparison failed') + '</div>'; return; }
    const data = await r.json();

    let html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px">';
    for (const p of data.panels) {
      html += '<div class="ab-panel">';
      if (p.error) {
        html += '<h3>' + esc(p.plugin_name) + '</h3>';
        html += '<div style="color:var(--danger);font-size:.82rem">' + esc(p.error) + '</div>';
      } else {
        html += '<h3>' + esc(p.label || p.plugin_name) + '</h3>';
        if (p.stale_reason) {
          html += '<div style="font-size:.72rem;color:var(--warning);margin-bottom:4px">Stale: ' + esc(p.stale_reason.replace(/_/g, ' ')) + '</div>';
        }
        const metrics = p.metrics || [];
        for (const m of metrics) {
          html += '<div class="metric"><span>' + esc(m.label || m.name) + '</span>'
            + '<span><strong>' + esc(String(m.value)) + '</strong>'
            + (m.unit ? ' ' + esc(m.unit) : '') + '</span></div>';
        }
        const insights = p.insights || [];
        for (const i of insights) {
          const cls = i.severity === 'critical' ? 'insight-critical'
            : i.severity === 'warning' ? 'insight-warning' : '';
          html += '<div class="insight ' + cls + '">' + esc(i.message) + '</div>';
        }
      }
      html += '</div>';
    }
    html += '</div>';
    panels.innerHTML = html;
  } catch (e) {
    panels.innerHTML = '<div style="color:var(--danger);font-size:.82rem">Error: ' + esc(e.message) + '</div>';
  }
}

// ---------------------------------------------------------------------------
// Polar Performance
// ---------------------------------------------------------------------------

let _polarData = null;

async function loadPolar() {
  try {
    const r = await fetch('/api/sessions/' + SESSION_ID + '/polar');
    if (!r.ok) return;
    const data = await r.json();
    if (!data.cells || !data.cells.length) return;

    _polarData = data;
    document.getElementById('polar-card').style.display = '';
    renderPolarDiagram();
    renderPolarHeatmap();

    // Summary line
    const above = data.cells.filter(c => c.delta != null && c.delta > 0).length;
    const below = data.cells.filter(c => c.delta != null && c.delta < 0).length;
    const noBaseline = data.cells.filter(c => c.delta == null).length;
    const withDelta = data.cells.filter(c => c.delta != null);
    let avgDelta = 0;
    if (withDelta.length) {
      const totalWeight = withDelta.reduce((s, c) => s + c.samples, 0);
      avgDelta = withDelta.reduce((s, c) => s + c.delta * c.samples, 0) / totalWeight;
    }
    const sign = avgDelta >= 0 ? '+' : '';
    const summary = document.getElementById('polar-summary');
    summary.innerHTML =
      sign + avgDelta.toFixed(2) + ' kt weighted avg vs baseline &middot; '
      + above + ' bins above, ' + below + ' below'
      + (noBaseline ? ' &middot; ' + noBaseline + ' bins no baseline' : '')
      + ' &middot; ' + data.session_sample_count + ' samples'
      + ' &middot; <button onclick="rebuildPolarBaseline()" style="background:none;border:none;color:var(--accent);cursor:pointer;font-size:.78rem;text-decoration:underline;padding:0">Rebuild baseline</button>';
  } catch (e) { /* non-fatal */ }
}

async function rebuildPolarBaseline() {
  const summary = document.getElementById('polar-summary');
  summary.textContent = 'Rebuilding baseline\u2026';
  try {
    const r = await fetch('/api/polar/rebuild', {method: 'POST'});
    if (!r.ok) { summary.textContent = 'Rebuild failed: ' + r.status; return; }
    const d = await r.json();
    summary.textContent = 'Baseline rebuilt: ' + d.bins + ' bins. Reloading\u2026';
    await loadPolar();
  } catch (e) { summary.textContent = 'Rebuild failed'; }
}

function setPolarView(view) {
  document.getElementById('polar-diagram-view').style.display = view === 'diagram' ? '' : 'none';
  document.getElementById('polar-heatmap-view').style.display = view === 'heatmap' ? '' : 'none';
  document.getElementById('polar-tab-diagram').classList.toggle('active', view === 'diagram');
  document.getElementById('polar-tab-heatmap').classList.toggle('active', view === 'heatmap');
}

// --- Polar diagram (Canvas) ---

let _TWS_COLORS = null;

function _initTwsColors() {
  if (_TWS_COLORS) return;
  _TWS_COLORS = [
    [6, cssVar('--accent')],       [8, cssVar('--accent-strong')], [10, cssVar('--accent-strong')],
    [12, '#7c3aed'],               [14, cssVar('--warning')],      [16, cssVar('--danger')],
    [18, cssVar('--danger')],      [20, '#991b1b'],
  ];
}

function _twsColor(tws) {
  _initTwsColors();
  for (let i = _TWS_COLORS.length - 1; i >= 0; i--) {
    if (tws >= _TWS_COLORS[i][0]) return _TWS_COLORS[i][1];
  }
  return cssVar('--text-muted');
}

function renderPolarDiagram() {
  const canvas = document.getElementById('polar-canvas');
  if (!canvas || !_polarData) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const cx = W / 2, cy = 30;
  const maxRadius = H - 50;

  ctx.clearRect(0, 0, W, H);

  // Determine BSP range for scaling
  let maxBsp = 0;
  for (const c of _polarData.cells) {
    if (c.session_mean != null) maxBsp = Math.max(maxBsp, c.session_mean);
    if (c.baseline_mean != null) maxBsp = Math.max(maxBsp, c.baseline_mean);
    if (c.baseline_p90 != null) maxBsp = Math.max(maxBsp, c.baseline_p90);
  }
  maxBsp = Math.ceil(maxBsp) + 1;
  if (maxBsp < 4) maxBsp = 4;
  const scale = maxRadius / maxBsp;

  // Draw concentric BSP circles
  const polarBorder = cssVar('--border');
  const polarTextSec = cssVar('--text-secondary');
  ctx.strokeStyle = polarBorder;
  ctx.lineWidth = 0.5;
  ctx.setLineDash([3, 3]);
  ctx.font = '11px monospace';
  ctx.fillStyle = polarTextSec;
  for (let bsp = 1; bsp <= maxBsp; bsp++) {
    const r = bsp * scale;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI);
    ctx.stroke();
    ctx.fillText(bsp + '', cx + r + 3, cy + 4);
  }

  // Draw radial TWA lines
  ctx.strokeStyle = polarBorder;
  for (let deg = 0; deg <= 180; deg += 30) {
    const rad = deg * Math.PI / 180;
    const x2 = cx + maxBsp * scale * Math.sin(rad);
    const y2 = cy + maxBsp * scale * Math.cos(rad);
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(x2, y2);
    ctx.stroke();
    // Label
    const lx = cx + (maxBsp * scale + 14) * Math.sin(rad);
    const ly = cy + (maxBsp * scale + 14) * Math.cos(rad);
    ctx.fillText(deg + '\u00b0', lx - 10, ly + 4);
  }
  ctx.setLineDash([]);

  // Group baseline cells by TWS
  const baselineByTws = {};
  for (const c of _polarData.cells) {
    if (c.baseline_mean == null) continue;
    if (!baselineByTws[c.tws]) baselineByTws[c.tws] = [];
    baselineByTws[c.tws].push(c);
  }

  // Draw baseline curves
  const drawnTws = [];
  for (const tws of Object.keys(baselineByTws).map(Number).sort((a, b) => a - b)) {
    const pts = baselineByTws[tws].sort((a, b) => a.twa - b.twa);
    if (pts.length < 2) continue;
    const color = _twsColor(tws);
    drawnTws.push({tws, color});

    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.globalAlpha = 0.7;
    ctx.beginPath();
    for (let i = 0; i < pts.length; i++) {
      const rad = pts[i].twa * Math.PI / 180;
      const r = pts[i].baseline_mean * scale;
      const x = cx + r * Math.sin(rad);
      const y = cy + r * Math.cos(rad);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // Draw session points
  for (const c of _polarData.cells) {
    if (c.session_mean == null) continue;
    const rad = c.twa * Math.PI / 180;
    const r = c.session_mean * scale;
    const x = cx + r * Math.sin(rad);
    const y = cy + r * Math.cos(rad);

    const dotColor = c.delta == null ? cssVar('--text-muted')
      : c.delta >= 0 ? cssVar('--success') : cssVar('--danger');
    const dotSize = Math.min(6, Math.max(3, Math.log2(c.samples + 1) * 1.5));

    ctx.beginPath();
    ctx.arc(x, y, dotSize, 0, 2 * Math.PI);
    ctx.fillStyle = dotColor;
    ctx.fill();
    ctx.strokeStyle = cssVar('--bg-primary');
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  // Legend
  const legend = document.getElementById('polar-legend');
  if (legend && drawnTws.length) {
    legend.innerHTML = 'Baseline curves: '
      + drawnTws.map(d =>
        '<span style="color:' + d.color + '">\u25cf ' + d.tws + ' kt</span>'
      ).join(' &nbsp; ')
      + ' &nbsp; Session: '
      + '<span style="color:var(--success)">\u25cf faster</span> '
      + '<span style="color:var(--danger)">\u25cf slower</span> '
      + '<span style="color:var(--text-muted)">\u25cf no baseline</span>';
  }
}

// --- Heatmap ---

function _deltaColor(delta) {
  if (delta == null) return cssVar('--bg-secondary');
  const clamped = Math.max(-1, Math.min(1, delta));
  if (clamped >= 0) {
    const t = clamped;
    return 'rgb(' + Math.round(20 + (1 - t) * 180) + ','
      + Math.round(80 + t * 160) + ','
      + Math.round(20 + (1 - t) * 180) + ')';
  } else {
    const t = -clamped;
    return 'rgb(' + Math.round(80 + t * 160) + ','
      + Math.round(20 + (1 - t) * 180) + ','
      + Math.round(20 + (1 - t) * 180) + ')';
  }
}

function renderPolarHeatmap() {
  const container = document.getElementById('polar-heatmap');
  if (!container || !_polarData) return;

  const data = _polarData;
  const cellMap = {};
  for (const c of data.cells) {
    cellMap[c.tws + ',' + c.twa] = c;
  }

  let html = '<table style="border-collapse:collapse;font-size:.72rem;width:100%">';

  // Header row: TWA labels
  html += '<tr><th style="padding:2px 4px;color:var(--text-secondary);text-align:right;font-weight:normal">TWS\\TWA</th>';
  for (const twa of data.twa_bins) {
    html += '<th style="padding:2px 4px;color:var(--text-secondary);font-weight:normal;min-width:36px">' + twa + '\u00b0</th>';
  }
  html += '</tr>';

  // One row per TWS
  for (const tws of data.tws_bins) {
    html += '<tr><td style="padding:2px 4px;color:var(--text-secondary);text-align:right;white-space:nowrap">' + tws + ' kt</td>';
    for (const twa of data.twa_bins) {
      const c = cellMap[tws + ',' + twa];
      if (!c) {
        html += '<td style="padding:2px 4px;background:var(--bg-secondary);border:1px solid var(--bg-input)"></td>';
        continue;
      }
      const bg = _deltaColor(c.delta);
      const textColor = c.delta == null ? 'var(--text-secondary)' : 'var(--text-primary)';
      const text = c.delta != null
        ? (c.delta >= 0 ? '+' : '') + c.delta.toFixed(2)
        : c.session_mean != null ? c.session_mean.toFixed(1) : '';
      const title = 'TWS=' + tws + ' TWA=' + twa + '\u00b0'
        + '\nSession BSP: ' + (c.session_mean != null ? c.session_mean.toFixed(2) : 'n/a')
        + '\nBaseline: ' + (c.baseline_mean != null ? c.baseline_mean.toFixed(2) : 'n/a')
        + '\nP90: ' + (c.baseline_p90 != null ? c.baseline_p90.toFixed(2) : 'n/a')
        + '\nSamples: ' + c.samples;
      html += '<td style="padding:2px 4px;background:' + bg + ';border:1px solid var(--bg-input);'
        + 'color:' + textColor + ';text-align:center;cursor:default" title="' + title + '">'
        + text + '</td>';
    }
    html += '</tr>';
  }
  html += '</table>';
  container.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Maneuvers
// ---------------------------------------------------------------------------

const _MANEUVER_COLORS = { tack: cssVar('--accent-strong'), gybe: cssVar('--warning'), rounding: cssVar('--success') };
const _RANK_COLORS = { good: cssVar('--success'), bad: cssVar('--error'), avg: cssVar('--text-secondary') };
let _maneuverSort = { key: 'ts', dir: 1 };  // ts | type | duration_sec | distance_loss_m | loss_kts | turn_angle_deg
let _maneuverFilter = 'all';  // all | tack | gybe | rounding | good | bad
let _maneuverOverlay = false; // toggle for all-tacks-overlaid diagram
let _maneuverSelected = new Set(); // ids of maneuvers selected for overlay

function _parseUtc(iso) {
  if (!iso) return null;
  const s = (iso.endsWith('Z') || iso.includes('+')) ? iso : iso + 'Z';
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

function _fmtElapsed(iso) {
  const t = _parseUtc(iso);
  if (!t || !_session || !_session.start_utc) return '\u2014';
  const start = _parseUtc(_session.start_utc);
  if (!start) return '\u2014';
  const secs = Math.max(0, Math.round((t.getTime() - start.getTime()) / 1000));
  const mm = Math.floor(secs / 60);
  const ss = secs % 60;
  return '+' + String(mm).padStart(2, '0') + ':' + String(ss).padStart(2, '0');
}

async function loadManeuvers() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/maneuvers');
  if (!r.ok) return;
  _maneuvers = await r.json();
  // Default: select all maneuvers for overlay when a new list arrives.
  _maneuverSelected = new Set(_maneuvers.map((m, i) => m.id != null ? m.id : i));
  renderManeuverCard();
  if (_map && _maneuvers.length) _addManeuverMarkers();
}

function _manKey(m, idx) {
  return m.id != null ? m.id : idx;
}

function toggleManeuverSelected(keyStr) {
  // keyStr may be a number-as-string; normalise
  const key = isNaN(Number(keyStr)) ? keyStr : Number(keyStr);
  if (_maneuverSelected.has(key)) _maneuverSelected.delete(key);
  else _maneuverSelected.add(key);
  if (_maneuverOverlay) renderManeuverCard();
}

function setManeuverSelectAll(mode) {
  if (mode === 'all') {
    _maneuverSelected = new Set(_maneuvers.map((m, i) => _manKey(m, i)));
  } else if (mode === 'none') {
    _maneuverSelected = new Set();
  } else if (mode === 'filtered') {
    _maneuverSelected = new Set(_maneuverRows().map((m) => _manKey(m, _maneuvers.indexOf(m))));
  }
  renderManeuverCard();
}

function _maneuverRows() {
  const items = _maneuvers.filter(m => {
    if (_maneuverFilter === 'all') return true;
    if (_maneuverFilter === 'good' || _maneuverFilter === 'bad') return m.rank === _maneuverFilter;
    return m.type === _maneuverFilter;
  });
  const key = _maneuverSort.key, dir = _maneuverSort.dir;
  items.sort((a, b) => {
    let av = a[key], bv = b[key];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (key === 'ts') { av = new Date(av).getTime(); bv = new Date(bv).getTime(); }
    if (key === 'type') return dir * String(av).localeCompare(String(bv));
    return dir * (av - bv);
  });
  return items;
}

function setManeuverSort(key) {
  if (_maneuverSort.key === key) _maneuverSort.dir *= -1;
  else { _maneuverSort.key = key; _maneuverSort.dir = key === 'ts' ? 1 : -1; }
  renderManeuverCard();
}

function setManeuverFilter(f) {
  _maneuverFilter = f;
  renderManeuverCard();
}

function toggleManeuverOverlay() {
  _maneuverOverlay = !_maneuverOverlay;
  renderManeuverCard();
}

// ---------- Tack diagram rendering (SVG) ----------

function _trackBounds(tracks) {
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  tracks.forEach(tr => tr.forEach(p => {
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

function _renderTrackSvg(tracks, opts) {
  // tracks: array of { points, color, label, highlight?, maneuverIdx?, ghost? }
  opts = opts || {};
  const w = opts.width || 260;
  const h = opts.height || 200;
  const pad = 12;
  const interactive = !!opts.interactive;
  const pointSets = tracks.map(t => t.points).filter(p => p && p.length);
  if (!pointSets.length) return '';
  // Extend bounds so the ghost reference line (running along y) is visible
  // even when it extends beyond the actual track.
  const ghostYs = tracks.map(t => t.ghost).filter(v => v != null && !isNaN(v));
  const extras = ghostYs.map(y => ({ x: 0, y }));
  const allPoints = pointSets.concat([extras]);
  const b = _trackBounds(allPoints);
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

  // Wind-up frame: +y = upwind. Label it so the orientation is unambiguous.
  const windLabels = '<text x="' + (w / 2) + '" y="10" text-anchor="middle" font-size="9" fill="var(--text-secondary)">↑ upwind</text>'
    + '<text x="' + (w / 2) + '" y="' + (h - 12) + '" text-anchor="middle" font-size="9" fill="var(--text-secondary)">↓ downwind</text>';

  // "Climb the ladder" ghost references — dashed vertical segments from
  // the origin to each track's idealized upwind-progress endpoint. Lets
  // you see at a glance how far you *would* have made had the tack been
  // a zero-loss instant turn.
  const ghostLines = tracks.map(t => {
    if (t.ghost == null || isNaN(t.ghost)) return '';
    const gy1 = sy(0), gy2 = sy(t.ghost);
    return '<line x1="' + originX + '" y1="' + gy1 + '" x2="' + originX + '" y2="' + gy2
      + '" stroke="' + t.color + '" stroke-width="1" stroke-dasharray="3,3" opacity="0.6"/>'
      + '<circle cx="' + originX + '" cy="' + gy2 + '" r="2" fill="' + t.color + '" opacity="0.6"/>';
  }).join('');

  const paths = tracks.map(t => {
    if (!t.points || !t.points.length) return '';
    const d = t.points.map((p, i) => (i === 0 ? 'M' : 'L') + sx(p.x).toFixed(1) + ' ' + sy(p.y).toFixed(1)).join(' ');
    const width = t.highlight ? 2.5 : 1.4;
    const opacity = t.highlight ? 1 : 0.7;
    let attrs = 'fill="none" stroke="' + t.color + '" stroke-width="' + width + '" opacity="' + opacity + '" stroke-linecap="round"';
    if (interactive && t.maneuverIdx != null) {
      attrs += ' data-man-idx="' + t.maneuverIdx + '"'
        + ' style="pointer-events:stroke;cursor:pointer"'
        + ' onmouseenter="showOverlayTip(event,' + t.maneuverIdx + ')"'
        + ' onmouseleave="scheduleOverlayTipHide()"'
        + ' onclick="highlightManeuver(' + t.maneuverIdx + ')"';
    }
    return '<path d="' + d + '" ' + attrs + '/>';
  }).join('');

  // Invisible fat underlay to widen hover hit-target.
  const hoverUnderlay = interactive ? tracks.map(t => {
    if (!t.points || !t.points.length || t.maneuverIdx == null) return '';
    const d = t.points.map((p, i) => (i === 0 ? 'M' : 'L') + sx(p.x).toFixed(1) + ' ' + sy(p.y).toFixed(1)).join(' ');
    return '<path d="' + d + '" fill="none" stroke="rgba(0,0,0,0)" stroke-width="14"'
      + ' style="pointer-events:stroke;cursor:pointer"'
      + ' onmouseenter="showOverlayTip(event,' + t.maneuverIdx + ')"'
      + ' onmouseleave="scheduleOverlayTipHide()"'
      + ' onclick="highlightManeuver(' + t.maneuverIdx + ')"/>';
  }).join('') : '';

  const scaleLabel = '<text x="' + (w - pad) + '" y="' + (h - 2) + '" text-anchor="end" font-size="9" fill="var(--text-secondary)">grid ' + gridStep + ' m</text>';

  return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:3px">'
    + gridLines.join('') + ghostLines + hoverUnderlay + paths + crosshair + windLabels + scaleLabel + '</svg>';
}

// Overlay tooltip — anchored on mouseenter, stays reachable by the mouse.
// Hiding is delayed so the cursor can traverse from the trace to the tip;
// entering the tip itself cancels the hide, so links inside are clickable.
let _overlayTipHideTimer = null;
let _overlayTipIdx = null;

function _ensureOverlayTip() {
  let tip = document.getElementById('overlay-tip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'overlay-tip';
    tip.style.cssText = 'position:fixed;z-index:9999;background:var(--bg-primary);'
      + 'border:1px solid var(--border);border-radius:4px;padding:6px 8px;font-size:.72rem;'
      + 'box-shadow:0 4px 12px rgba(0,0,0,0.3);max-width:240px;display:none';
    tip.onmouseenter = cancelOverlayTipHide;
    tip.onmouseleave = scheduleOverlayTipHide;
    document.body.appendChild(tip);
  }
  return tip;
}

function _highlightManeuverRow(idx, on) {
  // Mirror the overlay hover on the table row so the two views stay linked.
  document.querySelectorAll('.maneuver-table tr').forEach(r => r.classList.remove('hover-row'));
  if (on && idx != null) {
    const row = document.getElementById('mrow-' + idx);
    if (row) {
      row.classList.add('hover-row');
      row.scrollIntoView({block: 'nearest'});
    }
  }
}

function showOverlayTip(ev, idx) {
  cancelOverlayTipHide();
  const m = _maneuvers[idx];
  if (!m) return;
  _overlayTipIdx = idx;
  _highlightManeuverRow(idx, true);
  const tip = _ensureOverlayTip();
  const color = _MANEUVER_COLORS[m.type] || 'var(--text-secondary)';
  const rankColor = m.rank ? _RANK_COLORS[m.rank] : 'var(--text-secondary)';
  const twsVal = m.entry_tws != null ? m.entry_tws : (m.tws_bin != null ? m.tws_bin : null);
  const twsStr = twsVal != null ? ((twsVal.toFixed ? twsVal.toFixed(1) : twsVal) + ' kt') : '—';
  const rows = [
    ['Elapsed', _fmtElapsed(m.ts)],
    ['Time', fmtTime(m.ts)],
    ['Duration', m.duration_sec != null ? m.duration_sec.toFixed(1) + ' s' : '—'],
    ['Turn', m.turn_angle_deg != null ? Math.round(Math.abs(m.turn_angle_deg)) + '°' : '—'],
    ['BSP in→out', (m.entry_bsp != null ? m.entry_bsp.toFixed(1) : '—') + '→' + (m.exit_bsp != null ? m.exit_bsp.toFixed(1) : '—')],
    ['BSP dip', m.loss_kts != null ? m.loss_kts.toFixed(2) + ' kt' : '—'],
    ['Min BSP', m.min_bsp != null ? m.min_bsp.toFixed(1) + ' kt' : '—'],
    ['Dist loss', m.distance_loss_m != null ? m.distance_loss_m.toFixed(1) + ' m' : '—'],
    ['TWS', twsStr],
    ['TWD', m.twd_deg != null ? Math.round(m.twd_deg) + '°' : '—'],
  ];
  const header = '<div style="margin-bottom:4px;display:flex;justify-content:space-between;align-items:center;gap:6px">'
    + '<span><span style="color:' + color + ';font-weight:600">' + esc(m.type) + '</span>'
    + (m.rank ? ' <span style="color:' + rankColor + '">●' + esc(m.rank) + '</span>' : '') + '</span>'
    + '<span style="color:var(--text-secondary);cursor:pointer;font-size:.8rem" onclick="hideOverlayTip()" title="Close">✕</span>'
    + '</div>';
  const grid = '<div style="display:grid;grid-template-columns:auto 1fr;gap:2px 8px">'
    + rows.map(([k, v]) => '<span style="color:var(--text-secondary)">' + k + '</span><b>' + esc(v) + '</b>').join('')
    + '</div>';
  const yt = m.youtube_url
    ? '<div style="margin-top:6px"><a href="' + esc(m.youtube_url) + '" target="_blank" rel="noopener" style="color:var(--accent);text-decoration:none">&#9654; Watch on YouTube &#8599;</a></div>'
    : '';
  tip.innerHTML = header + grid + yt;
  tip.style.display = 'block';

  // Anchor the tooltip to the overlay SVG (top-right of the container) so
  // it does not follow the cursor. This keeps the YouTube link reachable
  // and prevents the "tooltip runs away" problem.
  const svgContainer = document.querySelector('#maneuvers-body svg');
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 14;
  let y = ev.clientY + 14;
  if (svgContainer) {
    const box = svgContainer.getBoundingClientRect();
    x = box.right + 8;
    y = box.top;
  }
  if (x + r.width > window.innerWidth) x = Math.max(2, window.innerWidth - r.width - 4);
  if (y + r.height > window.innerHeight) y = Math.max(2, window.innerHeight - r.height - 4);
  tip.style.left = x + 'px';
  tip.style.top = y + 'px';
}

function scheduleOverlayTipHide() {
  cancelOverlayTipHide();
  _overlayTipHideTimer = setTimeout(hideOverlayTip, 180);
}

function cancelOverlayTipHide() {
  if (_overlayTipHideTimer) {
    clearTimeout(_overlayTipHideTimer);
    _overlayTipHideTimer = null;
  }
}

function hideOverlayTip() {
  cancelOverlayTipHide();
  const tip = document.getElementById('overlay-tip');
  if (tip) tip.style.display = 'none';
  _highlightManeuverRow(_overlayTipIdx, false);
  _overlayTipIdx = null;
}

function _renderOverlaySvg() {
  const items = _maneuvers
    .filter((m, i) => _maneuverSelected.has(_manKey(m, i)))
    .filter(m => m.track && m.track.length);
  if (!items.length) {
    return '<div style="color:var(--text-secondary);font-size:.75rem">No maneuvers selected for overlay. Tick rows below to include them.</div>';
  }
  const tracks = items.map(m => ({
    points: m.track,
    color: _RANK_COLORS[m.rank] || _MANEUVER_COLORS[m.type] || 'var(--text-secondary)',
    label: m.type,
    highlight: false,
    maneuverIdx: _maneuvers.indexOf(m),
    ghost: m.ghost_m,
  }));
  const svg = _renderTrackSvg(tracks, { width: 420, height: 340, interactive: true });
  const legend = '<div style="font-size:.7rem;color:var(--text-secondary);margin-top:4px">'
    + items.length + ' of ' + _maneuvers.length + ' overlaid. Colours = rank '
    + '<span style="color:' + _RANK_COLORS.good + '">●good</span> '
    + '<span style="color:' + _RANK_COLORS.avg + '">●avg</span> '
    + '<span style="color:' + _RANK_COLORS.bad + '">●bad</span>. '
    + 'Entry at origin (+), entry direction ↑. Hover a trace for stats &amp; video.'
    + '</div>';
  return svg + legend;
}

function _manHeader(label, key) {
  const arrow = _maneuverSort.key === key ? (_maneuverSort.dir > 0 ? ' ▲' : ' ▼') : '';
  return '<th style="cursor:pointer" onclick="setManeuverSort(\'' + key + '\')">' + label + arrow + '</th>';
}

function renderManeuverCard() {
  const card = document.getElementById('maneuvers-card');
  const body = document.getElementById('maneuvers-body');
  card.style.display = '';

  if (!_maneuvers.length) {
    body.innerHTML = '<span style="color:var(--text-secondary)">No maneuvers detected. Click &#8635; Detect to analyse this session.</span>';
    return;
  }

  const tacks = _maneuvers.filter(m => m.type === 'tack').length;
  const gybes = _maneuvers.filter(m => m.type === 'gybe').length;
  const roundings = _maneuvers.filter(m => m.type === 'rounding').length;
  const good = _maneuvers.filter(m => m.rank === 'good').length;
  const bad = _maneuvers.filter(m => m.rank === 'bad').length;

  const overlayBtnStyle = 'font-size:.7rem;padding:2px 8px;border:1px solid var(--border);background:'
    + (_maneuverOverlay ? 'var(--accent)' : 'transparent') + ';color:'
    + (_maneuverOverlay ? 'var(--bg-primary)' : 'var(--text-secondary)') + ';cursor:pointer;border-radius:3px';
  const summary = '<div style="color:var(--text-secondary);font-size:.75rem;margin-bottom:6px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
    + '<span>' + tacks + 'T · ' + gybes + 'G · ' + roundings + 'R</span>'
    + '<span style="color:' + _RANK_COLORS.good + '">' + good + ' good</span>'
    + '<span style="color:' + _RANK_COLORS.bad + '">' + bad + ' bad</span>'
    + '<span style="flex:1"></span>'
    + '<button style="' + overlayBtnStyle + '" onclick="toggleManeuverOverlay()" title="Overlay all filtered tacks on one diagram">overlay</button>'
    + '<a href="/api/sessions/' + SESSION_ID + '/maneuvers.csv" download style="color:var(--accent);text-decoration:none">CSV &#8595;</a>'
    + '</div>';

  const filters = ['all', 'tack', 'gybe', 'rounding', 'good', 'bad'];
  const filterBar = '<div style="display:flex;gap:4px;margin-bottom:6px;flex-wrap:wrap">'
    + filters.map(f => {
        const active = _maneuverFilter === f;
        const style = 'font-size:.7rem;padding:2px 8px;border:1px solid var(--border);background:'
          + (active ? 'var(--accent)' : 'transparent') + ';color:'
          + (active ? 'var(--bg-primary)' : 'var(--text-secondary)') + ';cursor:pointer;border-radius:3px';
        return '<button style="' + style + '" onclick="setManeuverFilter(\'' + f + '\')">' + f + '</button>';
      }).join('')
    + '</div>';

  const items = _maneuverRows();
  let rows = items.map((m) => {
    const idx = _maneuvers.indexOf(m);
    const key = _manKey(m, idx);
    const color = _MANEUVER_COLORS[m.type] || 'var(--text-secondary)';
    const rankColor = m.rank ? _RANK_COLORS[m.rank] : 'transparent';
    const rankDot = m.rank
      ? '<span title="' + m.rank + '" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + rankColor + ';margin-right:4px"></span>'
      : '';
    const typeBadge = rankDot + '<span style="color:' + color + ';font-weight:600">' + esc(m.type) + '</span>';
    const selected = _maneuverSelected.has(key);
    const cbox = '<input type="checkbox" ' + (selected ? 'checked ' : '') + 'onclick="event.stopPropagation();toggleManeuverSelected(\'' + key + '\')" title="Include in overlay">';
    const elapsed = _fmtElapsed(m.ts);
    const t = fmtTime(m.ts);
    const dur = m.duration_sec != null ? m.duration_sec.toFixed(1) + 's' : '—';
    const turn = m.turn_angle_deg != null ? Math.round(Math.abs(m.turn_angle_deg)) + '°' : '—';
    const bspDip = m.loss_kts != null ? m.loss_kts.toFixed(2) + ' kt' : '—';
    const distLoss = m.distance_loss_m != null ? m.distance_loss_m.toFixed(1) + ' m' : '—';
    const entry = (m.entry_bsp != null ? m.entry_bsp.toFixed(1) : '—') + '→' + (m.exit_bsp != null ? m.exit_bsp.toFixed(1) : '—');
    // Fall back to the detector's stored tws_bin (integer kt) when the
    // averaged entry window didn't hit any wind samples.
    const twsVal = m.entry_tws != null ? m.entry_tws : (m.tws_bin != null ? m.tws_bin : null);
    const cond = twsVal != null ? (twsVal.toFixed ? twsVal.toFixed(0) : twsVal) + ' kt' : '—';
    const yt = m.youtube_url
      ? '<a href="' + esc(m.youtube_url) + '" target="_blank" rel="noopener" title="Watch on YouTube" style="color:var(--accent);text-decoration:none" onclick="event.stopPropagation()">&#9654;</a>'
      : '';
    return '<tr id="mrow-' + idx + '" style="cursor:pointer" onclick="highlightManeuver(' + idx + ')">'
      + '<td>' + cbox + '</td>'
      + '<td>' + typeBadge + '</td>'
      + '<td style="font-variant-numeric:tabular-nums">' + elapsed + '</td>'
      + '<td>' + t + '</td>'
      + '<td>' + dur + '</td>'
      + '<td>' + turn + '</td>'
      + '<td>' + entry + '</td>'
      + '<td title="BSP dip from pre-maneuver baseline to minimum BSP during the turn. Not exit−entry.">' + bspDip + '</td>'
      + '<td>' + distLoss + '</td>'
      + '<td>' + esc(cond) + '</td>'
      + '<td>' + yt + '</td>'
      + '</tr>';
  }).join('');

  const overlayBlock = _maneuverOverlay
    ? '<div style="margin-bottom:8px">' + _renderOverlaySvg() + '</div>'
    : '';

  const selCount = _maneuverSelected.size;
  const selectBar = '<div style="font-size:.7rem;color:var(--text-secondary);margin:4px 0;display:flex;gap:6px;align-items:center">'
    + '<span>Overlay: ' + selCount + ' selected</span>'
    + '<button style="font-size:.68rem;padding:1px 6px;border:1px solid var(--border);background:transparent;color:var(--text-secondary);cursor:pointer;border-radius:3px" onclick="setManeuverSelectAll(\'all\')">all</button>'
    + '<button style="font-size:.68rem;padding:1px 6px;border:1px solid var(--border);background:transparent;color:var(--text-secondary);cursor:pointer;border-radius:3px" onclick="setManeuverSelectAll(\'none\')">none</button>'
    + '<button style="font-size:.68rem;padding:1px 6px;border:1px solid var(--border);background:transparent;color:var(--text-secondary);cursor:pointer;border-radius:3px" onclick="setManeuverSelectAll(\'filtered\')">match filter</button>'
    + '</div>';

  body.innerHTML = summary + filterBar + overlayBlock + selectBar
    + '<table class="maneuver-table"><thead><tr>'
    + '<th title="Include in overlay"></th>'
    + _manHeader('Type', 'type')
    + '<th>Elapsed</th>'
    + _manHeader('Time', 'ts')
    + _manHeader('Dur', 'duration_sec')
    + _manHeader('Turn', 'turn_angle_deg')
    + '<th>BSP in→out</th>'
    + '<th title="BSP dip: baseline − min BSP during the turn. Not exit−entry." onclick="setManeuverSort(\'loss_kts\')" style="cursor:pointer">BSP dip' + (_maneuverSort.key === 'loss_kts' ? (_maneuverSort.dir > 0 ? ' ▲' : ' ▼') : '') + '</th>'
    + _manHeader('Dist loss', 'distance_loss_m')
    + '<th>TWS</th><th></th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table>'
    + '<div id="maneuver-detail" style="margin-top:8px"></div>';
}

function _renderManeuverDetail(m) {
  const el = document.getElementById('maneuver-detail');
  if (!el) return;
  if (!m) { el.innerHTML = ''; return; }
  const bspDipLabel = m.loss_kts != null && m.entry_bsp != null && m.min_bsp != null
    ? m.loss_kts.toFixed(2) + ' kt (' + m.entry_bsp.toFixed(1) + '→' + m.min_bsp.toFixed(1) + ')'
    : (m.loss_kts != null ? m.loss_kts.toFixed(2) + ' kt' : '—');
  const rows = [
    ['Entry HDG', m.entry_hdg != null ? m.entry_hdg.toFixed(0) + '°' : '—'],
    ['Exit HDG', m.exit_hdg != null ? m.exit_hdg.toFixed(0) + '°' : '—'],
    ['Turn rate', m.turn_rate_deg_s != null ? m.turn_rate_deg_s.toFixed(1) + '°/s' : '—'],
    ['Min BSP', m.min_bsp != null ? m.min_bsp.toFixed(1) + ' kt' : '—'],
    ['BSP dip', bspDipLabel],
    ['Entry TWA', m.entry_twa != null ? m.entry_twa.toFixed(0) + '°' : '—'],
    ['Exit TWA', m.exit_twa != null ? m.exit_twa.toFixed(0) + '°' : '—'],
    ['TWD', m.twd_deg != null ? Math.round(m.twd_deg) + '°' : '—'],
    ['Time to recover', m.time_to_recover_s != null ? m.time_to_recover_s.toFixed(1) + ' s' : '—'],
    ['Distance loss', m.distance_loss_m != null ? m.distance_loss_m.toFixed(1) + ' m' : '—'],
    ['Ghost upwind', m.ghost_m != null ? m.ghost_m.toFixed(1) + ' m' : '—'],
  ];
  const metricsGrid = '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px 12px;font-size:.72rem;background:var(--bg-secondary);padding:8px;border-radius:3px">'
    + rows.map(([k, v]) => '<div><span style="color:var(--text-secondary)">' + k + '</span> <b>' + esc(v) + '</b></div>').join('')
    + '</div>';
  const diagram = (m.track && m.track.length)
    ? _renderTrackSvg([{
        points: m.track,
        color: _RANK_COLORS[m.rank] || _MANEUVER_COLORS[m.type] || 'var(--accent)',
        label: m.type,
        highlight: true,
        ghost: m.ghost_m,
      }], { width: 300, height: 240 })
    : '';
  el.innerHTML = '<div style="display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap">'
    + '<div style="flex:1;min-width:260px">' + metricsGrid + '</div>'
    + (diagram ? '<div>' + diagram + '</div>' : '')
    + '</div>';
}

function _addManeuverMarkers() {
  // Remove old markers
  _maneuverMarkers.forEach(m => m.remove());
  _maneuverMarkers = [];

  _maneuvers.forEach((m, idx) => {
    if (m.lat == null || m.lon == null) return;
    const color = _MANEUVER_COLORS[m.type] || 'var(--text-secondary)';
    const marker = L.circleMarker([m.lat, m.lon], {
      radius: 7,
      color: color,
      fillColor: color,
      fillOpacity: 0.85,
      weight: 2,
    })
      .addTo(_map)
      .bindPopup(
        '<b style="color:' + color + '">' + m.type + '</b><br>'
        + fmtTime(m.ts)
        + (m.duration_sec != null ? '<br>' + m.duration_sec.toFixed(1) + ' s' : '')
        + (m.loss_kts != null ? '<br>' + m.loss_kts.toFixed(2) + ' kt loss' : '')
      );
    marker.on('click', function() { highlightManeuver(idx); });
    _maneuverMarkers.push(marker);
  });
}

function highlightManeuver(idx) {
  // Highlight table row
  document.querySelectorAll('.maneuver-table tr').forEach(r => r.classList.remove('active-row'));
  const row = document.getElementById('mrow-' + idx);
  if (row) {
    row.classList.add('active-row');
    row.scrollIntoView({block: 'nearest'});
  }
  const m = _maneuvers[idx];
  _renderManeuverDetail(m);
  // Move map cursor to maneuver position
  if (m && _trackData) {
    const ts = new Date(m.ts.endsWith('Z') || m.ts.includes('+') ? m.ts : m.ts + 'Z');
    setPosition(ts);
  }
  // Seek the embedded player to the maneuver moment if a video is loaded.
  if (m && _videoSync && _videoSync.player) {
    const ts = new Date(m.ts.endsWith('Z') || m.ts.includes('+') ? m.ts : m.ts + 'Z');
    const offset = _utcToVideoOffset(ts);
    if (offset != null && offset >= 0 && _videoSync.player.seekTo) {
      try { _videoSync.player.seekTo(offset, true); } catch (e) { /* ignore */ }
    }
  }
  // Open the marker popup if available
  if (_maneuverMarkers[idx]) _maneuverMarkers[idx].openPopup();
}

async function detectManeuvers() {
  const btn = document.getElementById('detect-maneuvers-btn');
  if (btn) { btn.textContent = '⏳'; btn.disabled = true; }
  try {
    const r = await fetch('/api/sessions/' + SESSION_ID + '/detect-maneuvers', {method: 'POST'});
    if (!r.ok) { alert('Detection failed: ' + r.status); return; }
    await loadManeuvers();
  } finally {
    if (btn) { btn.textContent = '↺ Detect'; btn.disabled = false; }
  }
}

// ---------------------------------------------------------------------------
// Wind Field visualization
// ---------------------------------------------------------------------------

let _wfMap = null;
let _wfCanvas = null;   // Leaflet canvas overlay
let _wfGrid = null;     // last fetched grid response
let _wfTimeseries = null;
let _wfPlaying = false;
let _wfPlayTimer = null;
let _wfTrackLine = null;
let _wfCursor = null;
let _wfMarkMarkers = [];
let _wfDuration = 0;
let _wfDebounce = null;

async function loadWindField() {
  const card = document.getElementById('wind-field-card');
  card.style.display = '';

  // Initialize the wind field Leaflet map
  _wfMap = L.map('wf-map');
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap', maxZoom: 18,
  }).addTo(_wfMap);

  // Overlay the boat track
  if (_trackData) {
    _wfTrackLine = L.polyline(_trackData.latLngs, {
      color: cssVar('--accent-strong'), weight: 3, opacity: 0.7,
    }).addTo(_wfMap);
    const wfCursorColor = cssVar('--warning');
    _wfCursor = L.circleMarker([0, 0], {
      radius: 6, color: wfCursorColor, fillColor: wfCursorColor, fillOpacity: 1, weight: 2,
    });
  }

  // Fetch initial grid (t=0) and timeseries in parallel
  const [gridR, tsR] = await Promise.all([
    fetch('/api/sessions/' + SESSION_ID + '/wind-field?elapsed_s=0&grid_size=25'),
    fetch('/api/sessions/' + SESSION_ID + '/wind-timeseries?step_s=10'),
  ]);
  if (!gridR.ok) { card.style.display = 'none'; return; }

  _wfGrid = await gridR.json();
  _wfTimeseries = tsR.ok ? await tsR.json() : null;
  _wfDuration = _wfGrid.duration_s;

  // Set slider range
  const slider = document.getElementById('wf-slider');
  slider.max = Math.floor(_wfDuration);
  slider.value = 0;
  slider.addEventListener('input', () => _onWfSlider(+slider.value));

  // Play button
  document.getElementById('wf-play-btn').addEventListener('click', _toggleWfPlay);

  // Draw marks
  _drawWfMarks(_wfGrid.marks);

  // Fit map bounds to grid
  _wfMap.fitBounds([
    [_wfGrid.grid.lat_min, _wfGrid.grid.lon_min],
    [_wfGrid.grid.lat_max, _wfGrid.grid.lon_max],
  ], {padding: [20, 20]});

  // Create canvas overlay
  _wfCanvas = _createWfCanvasOverlay();
  _wfCanvas.addTo(_wfMap);

  // Render initial state
  _renderWfGrid();
  if (_wfTimeseries) _renderWfChart(0);
}

function _drawWfMarks(marks) {
  for (const mm of _wfMarkMarkers) _wfMap.removeLayer(mm);
  _wfMarkMarkers = [];
  for (const m of marks) {
    const wfMarkColor = cssVar('--warning');
    const marker = L.circleMarker([m.lat, m.lon], {
      radius: 5, color: wfMarkColor, fillColor: wfMarkColor, fillOpacity: 0.9, weight: 1,
    }).addTo(_wfMap).bindTooltip(m.mark_name, {permanent: true, direction: 'right',
      className: 'wf-mark-label', offset: [8, 0]});
    _wfMarkMarkers.push(marker);
  }
}

// Custom Leaflet canvas overlay for wind field rendering
function _createWfCanvasOverlay() {
  const Overlay = L.Layer.extend({
    onAdd(map) {
      this._map = map;
      const pane = map.getPane('overlayPane');
      this._el = L.DomUtil.create('canvas', 'wf-overlay');
      this._el.style.position = 'absolute';
      this._el.style.pointerEvents = 'none';
      pane.appendChild(this._el);
      map.on('moveend zoomend resize', this._reset, this);
      this._reset();
    },
    onRemove(map) {
      L.DomUtil.remove(this._el);
      map.off('moveend zoomend resize', this._reset, this);
    },
    _reset() {
      const size = this._map.getSize();
      const topLeft = this._map.containerPointToLayerPoint([0, 0]);
      this._el.width = size.x;
      this._el.height = size.y;
      L.DomUtil.setPosition(this._el, topLeft);
      _renderWfGrid();
    },
    getCanvas() { return this._el; },
  });
  return new Overlay();
}

function _renderWfGrid() {
  if (!_wfCanvas || !_wfGrid) return;
  const canvas = _wfCanvas.getCanvas();
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const grid = _wfGrid.grid;
  const cells = grid.cells;
  const twsLow = _wfGrid.tws_low;
  const twsHigh = _wfGrid.tws_high;
  const rows = grid.rows;
  const cols = grid.cols;

  for (let i = 0; i < cells.length; i++) {
    const cell = cells[i];
    const pt = _wfMap.latLngToContainerPoint([cell.lat, cell.lon]);

    // Heatmap cell — color by TWS
    const norm = Math.max(0, Math.min(1, (cell.tws - twsLow) / (twsHigh - twsLow + 0.01)));
    const hue = 240 - norm * 240; // blue(low) -> red(high)
    ctx.fillStyle = 'hsla(' + hue + ', 80%, 50%, 0.35)';

    // Cell size in pixels
    const halfLat = (grid.lat_max - grid.lat_min) / (rows - 1) / 2;
    const halfLon = (grid.lon_max - grid.lon_min) / (cols - 1) / 2;
    const tl = _wfMap.latLngToContainerPoint([cell.lat + halfLat, cell.lon - halfLon]);
    const br = _wfMap.latLngToContainerPoint([cell.lat - halfLat, cell.lon + halfLon]);
    ctx.fillRect(tl.x, tl.y, br.x - tl.x, br.y - tl.y);

    // Wind arrow
    const arrowLen = 12;
    const twd_rad = cell.twd * Math.PI / 180;
    const dx = -Math.sin(twd_rad) * arrowLen;
    const dy = Math.cos(twd_rad) * arrowLen;
    ctx.strokeStyle = 'rgba(255,255,255,0.6)';
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(pt.x - dx * 0.5, pt.y - dy * 0.5);
    ctx.lineTo(pt.x + dx * 0.5, pt.y + dy * 0.5);
    ctx.stroke();
    // Arrowhead
    const ax = pt.x + dx * 0.5;
    const ay = pt.y + dy * 0.5;
    const headLen = 4;
    const angle = Math.atan2(dy, dx);
    ctx.beginPath();
    ctx.moveTo(ax, ay);
    ctx.lineTo(ax - headLen * Math.cos(angle - 0.5), ay - headLen * Math.sin(angle - 0.5));
    ctx.moveTo(ax, ay);
    ctx.lineTo(ax - headLen * Math.cos(angle + 0.5), ay - headLen * Math.sin(angle + 0.5));
    ctx.stroke();
  }

  // Draw cursor on track at current elapsed_s
  if (_wfCursor && _trackData && _wfGrid.elapsed_s != null && _wfGrid.duration_s > 0) {
    const frac = _wfGrid.elapsed_s / _wfGrid.duration_s;
    const idx = Math.min(Math.floor(frac * _trackData.latLngs.length), _trackData.latLngs.length - 1);
    _wfCursor.setLatLng(_trackData.latLngs[idx]).addTo(_wfMap);
  }
}

function _onWfSlider(val) {
  document.getElementById('wf-time-label').textContent = _fmtMmSs(val);
  if (_wfTimeseries) _renderWfChart(val);

  // Debounce API call for grid fetch
  if (_wfDebounce) clearTimeout(_wfDebounce);
  _wfDebounce = setTimeout(async () => {
    const r = await fetch('/api/sessions/' + SESSION_ID + '/wind-field?elapsed_s=' + val + '&grid_size=25');
    if (r.ok) {
      _wfGrid = await r.json();
      _renderWfGrid();
    }
  }, 150);
}

function _toggleWfPlay() {
  const btn = document.getElementById('wf-play-btn');
  const slider = document.getElementById('wf-slider');
  if (_wfPlaying) {
    _wfPlaying = false;
    btn.innerHTML = '&#9654;';
    if (_wfPlayTimer) { clearInterval(_wfPlayTimer); _wfPlayTimer = null; }
  } else {
    _wfPlaying = true;
    btn.innerHTML = '&#9646;&#9646;';
    _wfPlayTimer = setInterval(() => {
      let v = +slider.value + 10;
      if (v > +slider.max) v = 0;
      slider.value = v;
      _onWfSlider(v);
    }, 200);
  }
}

function _fmtMmSs(totalS) {
  const m = Math.floor(totalS / 60);
  const s = Math.floor(totalS % 60);
  return m + ':' + (s < 10 ? '0' : '') + s;
}

// Comparative wind time series chart (canvas-drawn)
function _renderWfChart(currentS) {
  const canvas = document.getElementById('wf-chart');
  if (!canvas || !_wfTimeseries) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  const pad = {l: 50, r: 20, t: 20, b: 30, mid: 20};
  const chartH = (H - pad.t - pad.b - pad.mid) / 2;
  const chartW = W - pad.l - pad.r;
  const series = _wfTimeseries.series;
  if (!series.length) return;

  const baseTwd = _wfTimeseries.base_twd;
  const dur = _wfTimeseries.duration_s;
  const colors = [cssVar('--danger'), cssVar('--text-primary'), cssVar('--success')]; // port, center, starboard

  // Compute TWD and TWS ranges
  let twdMin = Infinity, twdMax = -Infinity;
  let twsMin = Infinity, twsMax = -Infinity;
  for (const s of series) {
    for (const v of s.twd) { twdMin = Math.min(twdMin, v); twdMax = Math.max(twdMax, v); }
    for (const v of s.tws) { twsMin = Math.min(twsMin, v); twsMax = Math.max(twsMax, v); }
  }
  twdMin = Math.floor(twdMin - 2); twdMax = Math.ceil(twdMax + 2);
  twsMin = Math.floor(twsMin - 1); twsMax = Math.ceil(twsMax + 1);

  function xForT(t) { return pad.l + (t / dur) * chartW; }

  // --- TWD chart (top) ---
  const twdY0 = pad.t;
  function yForTwd(v) { return twdY0 + chartH - (v - twdMin) / (twdMax - twdMin) * chartH; }

  // Grid
  const wfBorder = cssVar('--border');
  const wfTextSec = cssVar('--text-secondary');
  ctx.strokeStyle = wfBorder; ctx.lineWidth = 0.5; ctx.setLineDash([3, 3]);
  for (let v = twdMin; v <= twdMax; v += 2) {
    const y = yForTwd(v);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + chartW, y); ctx.stroke();
  }
  ctx.setLineDash([]);

  // Axis labels
  ctx.fillStyle = wfTextSec; ctx.font = '11px monospace';
  ctx.fillText('TWD', pad.l - 40, twdY0 + chartH / 2 + 4);
  ctx.fillText(twdMin + '°', pad.l - 40, twdY0 + chartH - 2);
  ctx.fillText(twdMax + '°', pad.l - 40, twdY0 + 12);

  // Lines
  for (let p = 0; p < 3; p++) {
    ctx.strokeStyle = colors[p]; ctx.lineWidth = 1.5; ctx.globalAlpha = 0.85;
    ctx.beginPath();
    for (let i = 0; i < series.length; i++) {
      const x = xForT(series[i].t);
      const y = yForTwd(series[i].twd[p]);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // --- TWS chart (bottom) ---
  const twsY0 = pad.t + chartH + pad.mid;
  function yForTws(v) { return twsY0 + chartH - (v - twsMin) / (twsMax - twsMin) * chartH; }

  ctx.strokeStyle = wfBorder; ctx.lineWidth = 0.5; ctx.setLineDash([3, 3]);
  for (let v = twsMin; v <= twsMax; v += 2) {
    const y = yForTws(v);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + chartW, y); ctx.stroke();
  }
  ctx.setLineDash([]);

  ctx.fillStyle = wfTextSec;
  ctx.fillText('TWS', pad.l - 40, twsY0 + chartH / 2 + 4);
  ctx.fillText(twsMin + '', pad.l - 40, twsY0 + chartH - 2);
  ctx.fillText(twsMax + '', pad.l - 40, twsY0 + 12);

  for (let p = 0; p < 3; p++) {
    ctx.strokeStyle = colors[p]; ctx.lineWidth = 1.5; ctx.globalAlpha = 0.85;
    ctx.beginPath();
    for (let i = 0; i < series.length; i++) {
      const x = xForT(series[i].t);
      const y = yForTws(series[i].tws[p]);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // Time axis labels
  ctx.fillStyle = wfTextSec;
  const stepMin = Math.max(1, Math.floor(dur / 60 / 8));
  for (let m = 0; m <= dur / 60; m += stepMin) {
    const x = xForT(m * 60);
    ctx.fillText(m + 'm', x - 6, twsY0 + chartH + 14);
  }

  // Vertical hairline at current time
  if (currentS >= 0) {
    const x = xForT(currentS);
    ctx.strokeStyle = cssVar('--warning'); ctx.lineWidth = 1.5; ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, twsY0 + chartH); ctx.stroke();
  }

  // Legend
  const labels = ['Port', 'Center', 'Stbd'];
  let lx = pad.l + 10;
  for (let i = 0; i < 3; i++) {
    ctx.fillStyle = colors[i];
    ctx.fillRect(lx, pad.t - 14, 16, 8);
    ctx.fillStyle = wfTextSec;
    ctx.fillText(labels[i], lx + 20, pad.t - 6);
    lx += 70;
  }
}

// ---------------------------------------------------------------------------
// Boat Settings (read-only, time-synced)
// ---------------------------------------------------------------------------

let _bsParams = null;       // parameter definitions from /api/boat-settings/parameters
let _bsResolved = null;     // resolved settings at current playback time
let _bsLastAsOf = null;     // debounce: last as_of value we fetched
let _bsHistory = null;      // all race-specific setting entries (full timeline)

async function loadBoatSettings() {
  const card = document.getElementById('boat-settings-card');
  if (_session.type === 'debrief') return;
  card.style.display = '';

  try {
    const r = await fetch('/api/boat-settings/parameters');
    _bsParams = await r.json();
  } catch (e) { console.error('boat settings params error', e); return; }

  // For completed sessions use end time; for active sessions use now so values
  // entered during the session are visible rather than being filtered out.
  const asOf = _session.end_utc || new Date().toISOString();
  await _fetchAndRenderBoatSettings(asOf);
}

async function _fetchAndRenderBoatSettings(asOf) {
  if (!asOf || !_bsParams) return;
  _bsLastAsOf = asOf;
  try {
    const [resolveRes, historyRes] = await Promise.all([
      fetch('/api/boat-settings/resolve?race_id=' + SESSION_ID
        + '&as_of=' + encodeURIComponent(asOf)),
      fetch('/api/boat-settings?race_id=' + SESSION_ID),
    ]);
    if (resolveRes.ok) _bsResolved = await resolveRes.json();
    if (historyRes.ok) _bsHistory = await historyRes.json();
  } catch (e) { console.error('boat settings resolve error', e); return; }
  _renderBoatSettingsPanel();
}

function _renderBoatSettingsPanel() {
  const body = document.getElementById('boat-settings-body');
  if (!_bsParams || !_bsResolved) return;

  // Build lookup: parameter name → resolved entry (current value)
  const byParam = {};
  for (const entry of _bsResolved) byParam[entry.parameter] = entry;

  // Build lookup: parameter name → all race-specific history entries
  const histByParam = {};
  if (_bsHistory) {
    for (const entry of _bsHistory) {
      if (!histByParam[entry.parameter]) histByParam[entry.parameter] = [];
      histByParam[entry.parameter].push(entry);
    }
  }

  const fmtTs = (ts) => {
    if (!ts) return '';
    const d = new Date(ts);
    return d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  };

  const srcBadge = (entry) => {
    if (entry.race_id !== null) {
      const src = entry.source.startsWith('transcript') ? 'transcript' : entry.source;
      return '<span class="bs-source-badge ' + (entry.source.startsWith('transcript') ? 'transcript' : 'race') + '">' + esc(src) + '</span>';
    }
    return '<span style="color:' + cssVar('--text-muted') + ';font-size:.7rem">default</span>';
  };

  let html = '';

  for (const cat of _bsParams.categories) {
    // Crew weight row
    let crewWeightHtml = '';
    if (cat.category === 'crew' && _sessionCrew && _sessionCrew.length) {
      let totalBody = 0, totalGear = 0, hasW = false;
      for (const c of _sessionCrew) {
        if (c.body_weight != null) { totalBody += c.body_weight; hasW = true; }
        if (c.gear_weight != null) { totalGear += c.gear_weight; hasW = true; }
      }
      if (hasW) {
        const total = totalBody + totalGear;
        crewWeightHtml = '<div class="bs-row">'
          + '<span class="bs-label">Crew weight</span>'
          + '<span class="bs-value">' + total.toFixed(1) + '</span>'
          + '<span class="bs-unit">lbs</span>'
          + '<span style="color:var(--text-muted);font-size:.75rem;margin-left:6px">'
          + '(body ' + totalBody.toFixed(1) + ' + gear ' + totalGear.toFixed(1) + ')</span>'
          + '</div>';
      }
    }

    html += '<div class="setup-cat-header" onclick="toggleSetupCatSession(\'' + cat.category + '\')">';
    html += '<span class="setup-cat-label">' + esc(cat.label) + '</span>';
    html += '<span class="setup-cat-chevron" id="bs-cat-chev-' + cat.category + '">\u25BC</span>';
    html += '</div>';
    html += '<div class="setup-cat-body" id="bs-cat-' + cat.category + '">';
    html += crewWeightHtml;

    for (const p of cat.parameters) {
      const entry = byParam[p.name];
      const hist = histByParam[p.name] || [];
      const hasHistory = hist.length > 1 || (entry && entry.supersedes_value);
      const paramId = 'bs-hist-' + p.name;

      // Play button for transcript-sourced entries: compute audio offset from ts
      const playBtn = (e) => {
        if (!e.source || !e.source.startsWith('transcript') || !_session.audio_session_id || !e.ts || !_session.start_utc) return '';
        const offsetS = (Date.parse(e.ts) - Date.parse(_session.start_utc)) / 1000;
        if (offsetS < 0) return '';
        return '<button onclick="playSegmentAudio(' + offsetS.toFixed(1) + ',' + (offsetS + 8).toFixed(1) + ')" class="te-play-btn" title="Play transcript segment" style="margin-left:4px">&#9654;</button>';
      };

      // Current value row
      html += '<div class="bs-row" style="cursor:' + (hasHistory ? 'pointer' : 'default') + '"'
        + (hasHistory ? ' onclick="toggleBsHist(\'' + p.name + '\')"' : '') + '>';
      if (hasHistory) {
        html += '<span style="color:' + cssVar('--text-muted') + ';font-size:.7rem;margin-right:4px" id="bs-hist-chev-' + p.name + '">\u25B6</span>';
      }
      html += '<span class="bs-label">' + esc(p.label) + '</span>';
      if (entry) {
        html += '<span class="bs-value">' + esc(entry.value) + '</span>';
        if (p.unit) html += '<span class="bs-unit">' + esc(p.unit) + '</span>';
        html += srcBadge(entry);
        if (entry.ts) html += '<span style="color:' + cssVar('--text-muted') + ';font-size:.7rem;margin-left:6px" title="' + esc(entry.ts) + '">@ ' + fmtTs(entry.ts) + '</span>';
        html += playBtn(entry);
        if (hasHistory) html += '<span style="color:' + cssVar('--text-muted') + ';font-size:.7rem;margin-left:6px">(' + (hist.length + (entry.supersedes_value ? 1 : 0)) + ' entries)</span>';
      } else {
        html += '<span style="color:' + cssVar('--text-muted') + ';font-style:italic">not set</span>';
      }
      html += '</div>';

      // Collapsible history (hidden by default)
      if (hasHistory) {
        html += '<div id="' + paramId + '" style="display:none">';
        // Previous race-specific values, newest to oldest
        if (hist.length > 1) {
          for (let i = hist.length - 2; i >= 0; i--) {
            const h = hist[i];
            html += '<div class="bs-row" style="padding-left:24px;opacity:0.6">';
            html += '<span class="bs-label" style="font-size:.75rem">\u2514 previous</span>';
            html += '<span class="bs-value" style="font-size:.78rem">' + esc(h.value) + '</span>';
            if (p.unit) html += '<span class="bs-unit">' + esc(p.unit) + '</span>';
            html += srcBadge(h);
            if (h.ts) html += '<span style="color:' + cssVar('--text-muted') + ';font-size:.7rem;margin-left:6px" title="' + esc(h.ts) + '">@ ' + fmtTs(h.ts) + '</span>';
            html += playBtn(h);
            html += '</div>';
          }
        }
        // Superseded default at the bottom
        if (entry && entry.supersedes_value) {
          html += '<div class="bs-row" style="padding-left:24px;opacity:0.5">';
          html += '<span class="bs-label" style="font-size:.75rem">\u2514 default</span>';
          html += '<span class="bs-value" style="font-size:.78rem">' + esc(entry.supersedes_value) + '</span>';
          if (p.unit) html += '<span class="bs-unit">' + esc(p.unit) + '</span>';
          html += '<span style="color:' + cssVar('--text-muted') + ';font-size:.7rem">default</span>';
          html += '</div>';
        }
        html += '</div>';
      }
    }
    html += '</div>';
  }

  body.innerHTML = html;
}

function toggleBsHist(paramName) {
  const body = document.getElementById('bs-hist-' + paramName);
  const chev = document.getElementById('bs-hist-chev-' + paramName);
  if (!body) return;
  const hidden = body.style.display === 'none';
  body.style.display = hidden ? '' : 'none';
  if (chev) chev.textContent = hidden ? '\u25BC' : '\u25B6';
}

function toggleSetupCatSession(cat) {
  const body = document.getElementById('bs-cat-' + cat);
  const chev = document.getElementById('bs-cat-chev-' + cat);
  if (!body) return;
  const hidden = body.style.display === 'none';
  body.style.display = hidden ? '' : 'none';
  if (chev) chev.textContent = hidden ? '\u25BC' : '\u25B6';
}

// Called when the playback position changes (track click or video sync)
function _updateBoatSettingsForUtc(utcDate) {
  if (!_bsParams || !utcDate) return;
  const asOf = utcDate.toISOString();
  // Debounce: skip if same second
  if (_bsLastAsOf && _bsLastAsOf.slice(0, 19) === asOf.slice(0, 19)) return;
  _fetchAndRenderBoatSettings(asOf);
}

// ---------------------------------------------------------------------------
// Discussion threads (#282)
// ---------------------------------------------------------------------------

let _threads = [];
let _discussionMarkers = [];

function _threadTitle(t) {
  if (t.title) return esc(t.title);
  const body = t.first_comment_body || (t.comments && t.comments.length ? t.comments[0].body : null);
  if (body) return esc(body.length > 60 ? body.slice(0, 60) + '\u2026' : body);
  return 'Thread #' + t.id;
}

async function loadDiscussion() {
  const card = document.getElementById('discussion-card');
  card.style.display = '';
  const body = document.getElementById('discussion-body');
  const r = await fetch('/api/sessions/' + SESSION_ID + '/threads');
  if (!r.ok) { body.innerHTML = '<span style="color:var(--text-secondary)">Failed to load</span>'; return; }
  _threads = await r.json();
  const totalUnread = _threads.reduce((s, t) => s + (t.unread_count || 0), 0);
  const badge = document.getElementById('discussion-badge');
  badge.textContent = totalUnread > 0 ? '(' + totalUnread + ' unread)' : '';
  _addDiscussionMarkers();
  if (!_threads.length) {
    body.innerHTML = '<span style="color:var(--text-secondary)">No discussions yet. Start one with + New Thread above.</span>';
    return;
  }
  body.innerHTML = _threads.map(t => {
    const anchor = t.mark_reference
      ? '<span class="thread-anchor">' + esc(t.mark_reference.replace(/_/g, ' ')) + '</span>'
      : t.anchor_timestamp
        ? '<span class="thread-anchor" style="cursor:pointer;text-decoration:underline" '
          + 'onclick="event.stopPropagation();seekToThreadAnchor(\'' + esc(t.anchor_timestamp) + '\')" '
          + 'title="Seek playback to this moment">' + fmtTime(t.anchor_timestamp) + '</span>'
        : '';
    const unread = t.unread_count > 0
      ? '<span class="thread-unread">' + t.unread_count + '</span>'
      : '';
    const resolved = t.resolved ? ' resolved' : '';
    const resolvedTag = t.resolved ? '<span style="color:var(--success);font-size:.7rem;margin-left:6px">&#10003; Resolved</span>' : '';
    const title = _threadTitle(t);
    const author = t.author_name || t.author_email || 'Crew Member';
    const count = t.comment_count === 1 ? '1 comment' : t.comment_count + ' comments';
    const resolutionHtml = t.resolved && t.resolution_summary
      ? '<div style="background:var(--bg-secondary);border:1px solid var(--success);border-radius:4px;padding:4px 8px;margin-top:4px;font-size:.72rem;color:var(--success)">'
        + '<strong>Resolution:</strong> ' + esc(t.resolution_summary) + '</div>'
      : '';
    return '<div class="thread-item' + resolved + '" onclick="openThread(' + t.id + ')">'
      + '<div><strong style="color:var(--text-primary)">' + title + '</strong>' + anchor + unread + resolvedTag + '</div>'
      + '<div style="font-size:.72rem;color:var(--text-secondary);margin-top:2px">' + esc(author) + ' &middot; ' + count + ' &middot; ' + fmtTime(t.created_at) + '</div>'
      + resolutionHtml
      + '</div>';
  }).join('');
}

function seekToThreadAnchor(ts) {
  if (!ts) return;
  const utc = new Date(ts.endsWith('Z') || ts.includes('+') ? ts : ts + 'Z');
  if (isNaN(utc.getTime())) return;
  setPosition(utc);
}

function _checkThreadHash() {
  const hash = window.location.hash;
  const m = hash.match(/^#thread-(\d+)$/);
  if (m) {
    const threadId = parseInt(m[1], 10);
    openThread(threadId);
  }
}

function _addDiscussionMarkers() {
  _discussionMarkers.forEach(m => m.remove());
  _discussionMarkers = [];
  if (!_map || !_trackData) return;

  _threads.forEach(t => {
    if (!t.anchor_timestamp) return;
    const ts = new Date(t.anchor_timestamp.endsWith('Z') || t.anchor_timestamp.includes('+') ? t.anchor_timestamp : t.anchor_timestamp + 'Z');
    const idx = _indexForUtc(ts);
    const latLng = _trackData.latLngs[idx];
    if (!latLng) return;

    const title = _threadTitle(t);
    const unread = t.unread_count > 0 ? ' <span class="thread-unread">' + t.unread_count + '</span>' : '';
    const resolvedHtml = t.resolved
      ? '<div style="color:var(--success);font-size:.7rem;margin-top:2px">&#10003; Resolved</div>'
        + (t.resolution_summary
          ? '<div style="background:var(--bg-secondary);border:1px solid var(--success);border-radius:4px;padding:4px 6px;margin-top:3px;font-size:.7rem;color:var(--success)">'
            + esc(t.resolution_summary.length > 120 ? t.resolution_summary.slice(0, 120) + '\u2026' : t.resolution_summary) + '</div>'
          : '')
      : '';
    const author = t.author_name || t.author_email || 'Crew Member';
    const count = t.comment_count === 1 ? '1 comment' : t.comment_count + ' comments';

    const popup = '<div style="max-width:260px">'
      + '<div style="font-weight:600;color:var(--text-primary);font-size:.82rem">' + title + unread + '</div>'
      + '<div style="font-size:.7rem;color:var(--text-secondary)">' + esc(author) + ' &middot; ' + count + ' &middot; ' + fmtTime(t.anchor_timestamp) + '</div>'
      + resolvedHtml
      + '<div id="discussion-marker-preview-' + t.id + '">'
      + '<div style="font-size:.7rem;color:var(--text-secondary);margin-top:4px">Loading\u2026</div></div>'
      + '<div style="margin-top:6px"><a href="#" data-open-thread="' + t.id + '" '
      + 'style="color:var(--accent);font-size:.78rem;text-decoration:none">Open thread &rarr;</a></div>'
      + '</div>';

    const hasUnread = t.unread_count > 0;
    const markerColor = t.resolved ? cssVar('--success') : hasUnread ? cssVar('--accent') : cssVar('--text-secondary');
    const bgPrimary = cssVar('--bg-primary');
    const markerStyle = t.resolved
      ? 'width:14px;height:14px;background:transparent;border:2px solid ' + cssVar('--success') + ';border-radius:50%'
      : 'width:14px;height:14px;background:' + markerColor + ';border:2px solid ' + bgPrimary + ';border-radius:50%;box-shadow:0 0 4px ' + markerColor;
    const icon = L.divIcon({
      className: 'discussion-marker',
      html: '<div style="' + markerStyle + '"></div>',
      iconSize: [14, 14],
      iconAnchor: [7, 7],
    });
    const threadId = t.id;
    const marker = L.marker(latLng, {icon: icon})
      .addTo(_map)
      .bindPopup(popup, {maxWidth: 280, minWidth: 200});
    marker.on('popupopen', function() {
      _loadMarkerPreview(threadId);
      // Wire up the "Open thread" link after Leaflet renders the popup DOM
      const popupEl = marker.getPopup().getElement();
      if (popupEl) {
        const link = popupEl.querySelector('[data-open-thread]');
        if (link) {
          link.addEventListener('click', function(ev) {
            ev.preventDefault();
            marker.closePopup();
            openThread(threadId);
            document.getElementById('discussion-card').scrollIntoView({behavior: 'smooth', block: 'start'});
          });
        }
      }
    });
    _discussionMarkers.push(marker);
  });
}

async function _loadMarkerPreview(threadId) {
  const el = document.getElementById('discussion-marker-preview-' + threadId);
  if (!el) return;
  const r = await fetch('/api/threads/' + threadId);
  if (!r.ok) { el.innerHTML = ''; return; }
  const t = await r.json();
  const comments = (t.comments || []).slice(-3);
  if (!comments.length) {
    el.innerHTML = '<div style="font-size:.72rem;color:var(--text-secondary);margin-top:4px">No comments yet</div>';
    return;
  }
  el.innerHTML = comments.map(c => {
    const a = c.author_name || c.author_email || 'Crew Member';
    const body = c.body.length > 100 ? c.body.slice(0, 100) + '\u2026' : c.body;
    return '<div style="margin-top:4px;font-size:.72rem;border-left:2px solid ' + cssVar('--border') + ';padding-left:6px">'
      + '<span style="color:' + cssVar('--accent') + ';font-weight:600">' + esc(a) + '</span> '
      + '<span style="color:var(--text-primary)">' + esc(body) + '</span></div>';
  }).join('');
}

function showNewThreadForm(anchorTimestamp) {
  const body = document.getElementById('discussion-body');
  const form = document.createElement('div');
  form.className = 'thread-form';
  form.style.marginBottom = '10px';
  // Default anchor to the current playback position if the caller didn't pass one
  if (!anchorTimestamp && _playClock.positionUtc) {
    anchorTimestamp = _playClock.positionUtc.toISOString();
  }
  const anchorLabel = anchorTimestamp ? fmtTime(anchorTimestamp) : '';
  const anchorHidden = anchorTimestamp
    ? '<input type="hidden" id="new-thread-anchor-ts" value="' + esc(anchorTimestamp) + '"/>'
      + '<div id="new-thread-anchor-row" style="font-size:.72rem;color:var(--warning);margin-bottom:6px">'
      + 'Anchored at <span id="new-thread-anchor-label">' + anchorLabel + '</span> '
      + '<button type="button" onclick="clearNewThreadAnchor()" '
      + 'style="background:none;border:none;color:var(--text-secondary);cursor:pointer;font-size:.72rem;text-decoration:underline">clear</button>'
      + '</div>'
    : '<input type="hidden" id="new-thread-anchor-ts" value=""/>'
      + '<div id="new-thread-anchor-row" style="font-size:.72rem;color:var(--text-secondary);margin-bottom:6px">'
      + 'Race-general thread (no anchor) '
      + '<button type="button" onclick="useCurrentAnchor()" '
      + 'style="background:none;border:none;color:var(--accent);cursor:pointer;font-size:.72rem;text-decoration:underline">use current time</button>'
      + '</div>';
  form.innerHTML = anchorHidden
    + '<div style="display:flex;gap:6px;margin-bottom:6px">'
    + '<input id="new-thread-title" placeholder="Thread title (optional)" style="flex:1"/>'
    + '<select id="new-thread-mark" style="width:auto"><option value="">No mark anchor</option>'
    + '<option value="start">Start</option>'
    + '<option value="weather_mark_1">Weather Mark 1</option><option value="weather_mark_2">Weather Mark 2</option>'
    + '<option value="leeward_mark_1">Leeward Mark 1</option><option value="leeward_mark_2">Leeward Mark 2</option>'
    + '<option value="gate_1">Gate 1</option><option value="gate_2">Gate 2</option>'
    + '<option value="offset_mark_1">Offset Mark 1</option>'
    + '<option value="finish">Finish</option>'
    + '</select></div>'
    + '<textarea id="new-thread-body" placeholder="First comment\u2026"></textarea>'
    + '<div style="margin-top:6px;display:flex;gap:6px">'
    + '<button class="btn-thread" onclick="submitNewThread()">Create Thread</button>'
    + '<button class="btn-thread" style="background:none;color:var(--text-secondary)" onclick="loadDiscussion()">Cancel</button>'
    + '</div>';
  body.prepend(form);
}

function clearNewThreadAnchor() {
  const inp = document.getElementById('new-thread-anchor-ts');
  if (inp) inp.value = '';
  const row = document.getElementById('new-thread-anchor-row');
  if (row) {
    row.style.color = 'var(--text-secondary)';
    row.innerHTML = 'Race-general thread (no anchor) '
      + '<button type="button" onclick="useCurrentAnchor()" '
      + 'style="background:none;border:none;color:var(--accent);cursor:pointer;font-size:.72rem;text-decoration:underline">use current time</button>';
  }
}

function useCurrentAnchor() {
  if (!_playClock.positionUtc) return;
  const utc = _playClock.positionUtc.toISOString();
  const inp = document.getElementById('new-thread-anchor-ts');
  if (inp) inp.value = utc;
  const row = document.getElementById('new-thread-anchor-row');
  if (row) {
    row.style.color = 'var(--warning)';
    row.innerHTML = 'Anchored at <span id="new-thread-anchor-label">' + fmtTime(utc) + '</span> '
      + '<button type="button" onclick="clearNewThreadAnchor()" '
      + 'style="background:none;border:none;color:var(--text-secondary);cursor:pointer;font-size:.72rem;text-decoration:underline">clear</button>';
  }
}

async function submitNewThread() {
  const title = document.getElementById('new-thread-title').value.trim();
  const mark = document.getElementById('new-thread-mark').value || null;
  const anchorTs = document.getElementById('new-thread-anchor-ts').value || null;
  const firstComment = document.getElementById('new-thread-body').value.trim();
  const payload = {};
  if (title) payload.title = title;
  if (mark) payload.mark_reference = mark;
  if (anchorTs) payload.anchor_timestamp = anchorTs;
  const r = await fetch('/api/sessions/' + SESSION_ID + '/threads', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
  });
  if (!r.ok) { alert('Failed to create thread'); return; }
  const {id} = await r.json();
  if (firstComment) {
    await fetch('/api/threads/' + id + '/comments', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({body: firstComment})
    });
  }
  openThread(id);
}

async function openThread(threadId) {
  const body = document.getElementById('discussion-body');
  body.innerHTML = '<span style="color:var(--text-secondary)">Loading\u2026</span>';
  // Mark as read
  fetch('/api/threads/' + threadId + '/read', {method: 'POST'});
  const r = await fetch('/api/threads/' + threadId);
  if (!r.ok) { loadDiscussion(); return; }
  const t = await r.json();
  const title = _threadTitle(t);
  const anchor = t.mark_reference
    ? '<span class="thread-anchor">' + esc(t.mark_reference.replace(/_/g, ' ')) + '</span>'
    : t.anchor_timestamp
      ? '<span class="thread-anchor" style="cursor:pointer;text-decoration:underline" '
        + 'onclick="seekToThreadAnchor(\'' + esc(t.anchor_timestamp) + '\')" '
        + 'title="Seek playback to this moment">' + fmtTime(t.anchor_timestamp) + '</span>'
      : '';
  let resolveBtn = '';
  if (t.resolved) {
    resolveBtn = '<button class="btn-unresolve" onclick="unresolveThread(' + t.id + ')">Unresolve</button>';
  } else {
    resolveBtn = '<button class="btn-resolve" onclick="resolveThread(' + t.id + ')">Resolve</button>';
  }
  const resolutionHtml = t.resolved && t.resolution_summary
    ? '<div style="background:var(--bg-secondary);border:1px solid var(--success);border-radius:4px;padding:6px 8px;margin-top:6px;font-size:.78rem;color:var(--success)">'
      + '<strong>Resolution:</strong> ' + esc(t.resolution_summary) + '</div>'
    : '';
  const commentsHtml = (t.comments || []).map(c => {
    const author = c.author_name || c.author_email || 'Crew Member';
    const edited = c.edited_at ? ' <span class="comment-edited">(edited)</span>' : '';
    return '<div class="comment-item">'
      + '<span class="comment-author">' + esc(author) + '</span>'
      + '<span class="comment-time">' + fmtTime(c.created_at) + '</span>' + edited
      + '<div class="comment-body">' + _renderMentions(esc(c.body)) + '</div>'
      + '</div>';
  }).join('');
  body.innerHTML = '<div style="margin-bottom:8px">'
    + '<button style="background:none;border:none;color:var(--accent);cursor:pointer;font-size:.78rem;padding:0" onclick="loadDiscussion()">&larr; All threads</button>'
    + '</div>'
    + '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:6px">'
    + '<div style="flex:1;min-width:0"><strong style="color:var(--text-primary);font-size:.9rem">' + title + '</strong>' + anchor + '</div>'
    + '<div style="flex-shrink:0">' + resolveBtn + '</div>'
    + '</div>'
    + resolutionHtml
    + '<div id="thread-comments">' + (commentsHtml || '<span style="color:var(--text-secondary)">No comments yet</span>') + '</div>'
    + '<div class="thread-form" style="margin-top:8px">'
    + '<textarea id="reply-body" placeholder="Reply\u2026"></textarea>'
    + '<div style="margin-top:4px"><button class="btn-thread" onclick="submitReply(' + t.id + ')">Reply</button></div>'
    + '</div>';
  document.getElementById('discussion-card').scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function submitReply(threadId) {
  const el = document.getElementById('reply-body');
  const text = el.value.trim();
  if (!text) return;
  const r = await fetch('/api/threads/' + threadId + '/comments', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({body: text})
  });
  if (!r.ok) { alert('Failed to post reply'); return; }
  openThread(threadId);
}

function resolveThread(threadId) {
  const container = document.getElementById('thread-comments');
  if (!container) return;
  // Show inline form instead of prompt() — mobile browsers handle prompt() inconsistently
  const existing = document.getElementById('resolve-form');
  if (existing) { existing.remove(); return; }
  const form = document.createElement('div');
  form.id = 'resolve-form';
  form.className = 'thread-form';
  form.style.marginTop = '8px';
  form.innerHTML = '<textarea id="resolve-summary" placeholder="Resolution summary (optional)"></textarea>'
    + '<div style="margin-top:4px;display:flex;gap:6px">'
    + '<button class="btn-resolve" onclick="_submitResolve(' + threadId + ')">Confirm Resolve</button>'
    + '<button class="btn-thread" style="background:none;color:var(--text-secondary)" onclick="document.getElementById(\'resolve-form\').remove()">Cancel</button>'
    + '</div>';
  container.after(form);
  document.getElementById('resolve-summary').focus();
}

async function _submitResolve(threadId) {
  const el = document.getElementById('resolve-summary');
  const summary = el ? el.value.trim() || null : null;
  await fetch('/api/threads/' + threadId + '/resolve', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({resolution_summary: summary})
  });
  openThread(threadId);
}

async function unresolveThread(threadId) {
  await fetch('/api/threads/' + threadId + '/unresolve', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
  });
  openThread(threadId);
}

// ---------------------------------------------------------------------------
// @mention autocomplete (#284)
// ---------------------------------------------------------------------------

let _mentionUsers = null; // [{id, name}, ...]

function _renderMentions(escapedText) {
  if (!_mentionUsers || !_mentionUsers.length) {
    // Fallback: highlight single-word @mentions
    return escapedText.replace(/@([\w.\-]+)/g, '<span style="color:var(--accent);font-weight:600">@$1</span>');
  }
  // Sort names longest-first so "dan weatbrook" matches before "dan"
  const names = _mentionUsers.map(u => u.name).filter(Boolean).sort((a, b) => b.length - a.length);
  let result = escapedText;
  for (const name of names) {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    result = result.replace(
      new RegExp('@' + escaped, 'g'),
      '<span style="color:var(--accent);font-weight:600">@' + name + '</span>'
    );
  }
  return result;
}

async function _loadMentionUsers() {
  if (_mentionUsers) return _mentionUsers;
  try {
    const r = await fetch('/api/users/names');
    if (r.ok) _mentionUsers = await r.json();
    else _mentionUsers = [];
  } catch (e) { _mentionUsers = []; }
  return _mentionUsers;
}

function _getMentionContext(el) {
  const val = el.value;
  const pos = el.selectionStart;
  // Walk backward from cursor to find @ — allow spaces for multi-word names
  let i = pos - 1;
  while (i >= 0 && /[\w.\- ]/.test(val[i])) i--;
  if (i < 0 || val[i] !== '@') return null;
  // Don't trigger if @ is preceded by a word char (e.g. email)
  if (i > 0 && /\w/.test(val[i - 1])) return null;
  const query = val.substring(i + 1, pos);
  // Don't match if query is only whitespace after @
  if (!query.replace(/\s/g, '')) return { start: i, end: pos, query: '' };
  return { start: i, end: pos, query };
}

function _insertMention(el, ctx, name) {
  const before = el.value.substring(0, ctx.start);
  const after = el.value.substring(ctx.end);
  el.value = before + '@' + name + ' ' + after;
  const newPos = ctx.start + name.length + 2;
  el.setSelectionRange(newPos, newPos);
  el.focus();
  _removeMentionDropdown();
}

function _removeMentionDropdown() {
  const existing = document.getElementById('mention-dropdown');
  if (existing) existing.remove();
}

function _showMentionDropdown(el, matches, ctx) {
  _removeMentionDropdown();
  if (!matches.length) return;

  const dd = document.createElement('div');
  dd.id = 'mention-dropdown';
  dd.style.cssText = 'position:absolute;z-index:9999;background:var(--bg-secondary);border:1px solid var(--accent-strong);'
    + 'border-radius:6px;max-height:150px;overflow-y:auto;min-width:160px;box-shadow:0 4px 12px rgba(0,0,0,.5)';

  const capped = matches.slice(0, 8);
  capped.forEach((u, idx) => {
    const item = document.createElement('div');
    item.textContent = u.name;
    item.setAttribute('data-mention-item', '');
    item.style.cssText = 'padding:6px 10px;cursor:pointer;font-size:.82rem;color:var(--text-primary)';
    if (idx === 0) item.style.background = cssVar('--border');
    item.addEventListener('mouseenter', () => { _highlightMentionItem(idx); });
    item.addEventListener('mousedown', (e) => {
      e.preventDefault();
      _insertMention(el, ctx, u.name);
    });
    dd.appendChild(item);
  });

  // Position below the textarea
  const rect = el.getBoundingClientRect();
  dd.style.left = rect.left + 'px';
  dd.style.top = (rect.bottom + 2) + 'px';
  dd.style.position = 'fixed';
  document.body.appendChild(dd);
}

let _mentionActiveEl = null;
let _mentionIdx = 0;

function _highlightMentionItem(idx) {
  const dd = document.getElementById('mention-dropdown');
  if (!dd) return;
  const items = dd.querySelectorAll('[data-mention-item]');
  items.forEach((el, i) => {
    el.style.background = i === idx ? cssVar('--border') : 'none';
  });
  _mentionIdx = idx;
  if (items[idx]) items[idx].scrollIntoView({block: 'nearest'});
}

function _handleMentionInput(e) {
  const el = e.target;
  if (el.tagName !== 'TEXTAREA') return;
  _mentionActiveEl = el;
  const ctx = _getMentionContext(el);
  if (!ctx) { _removeMentionDropdown(); return; }
  _mentionIdx = 0;
  _loadMentionUsers().then(users => {
    const q = ctx.query.toLowerCase();
    const matches = users.filter(u => u.name && u.name.toLowerCase().includes(q));
    _showMentionDropdown(el, matches, ctx);
  });
}

function _handleMentionKeydown(e) {
  const dd = document.getElementById('mention-dropdown');
  if (!dd) return;
  const items = dd.querySelectorAll('[data-mention-item]');
  if (!items.length) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    _removeMentionDropdown();
    return;
  }
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    _highlightMentionItem(Math.min(_mentionIdx + 1, items.length - 1));
    return;
  }
  if (e.key === 'ArrowUp') {
    e.preventDefault();
    _highlightMentionItem(Math.max(_mentionIdx - 1, 0));
    return;
  }
  if (e.key === 'Tab' || e.key === 'Enter') {
    const active = items[_mentionIdx];
    if (active && _mentionActiveEl) {
      e.preventDefault();
      const ctx = _getMentionContext(_mentionActiveEl);
      if (ctx) _insertMention(_mentionActiveEl, ctx, active.textContent);
    }
  }
}

// Use event delegation on the discussion card
document.addEventListener('input', _handleMentionInput);
document.addEventListener('keydown', _handleMentionKeydown);
document.addEventListener('click', (e) => {
  if (!e.target.closest('#mention-dropdown')) _removeMentionDropdown();
});

// ---------------------------------------------------------------------------
// Tuning Extraction
// ---------------------------------------------------------------------------

async function loadTuningExtractions() {
  if (!_transcriptId) return;
  const r = await fetch('/api/tuning/runs?transcript_id=' + _transcriptId);
  if (!r.ok) return;
  const runs = await r.json();
  renderTuningExtractions(runs);
}

async function renderTuningExtractions(runs) {
  const card = document.getElementById('tuning-extraction-card');
  const body = document.getElementById('tuning-extraction-body');
  const badge = document.getElementById('tuning-extraction-badge');
  card.style.display = '';

  if (!runs.length) {
    badge.textContent = '';
    body.innerHTML = '<span style="color:' + cssVar('--text-secondary') + '">No tuning changes extracted yet. Click &#8635; Extract to analyse the transcript.</span>';
    return;
  }

  // Fetch full details for each run (includes items)
  const detailed = [];
  for (const run of runs) {
    const dr = await fetch('/api/tuning/runs/' + run.id);
    if (dr.ok) detailed.push(await dr.json());
  }

  const totalItems = detailed.reduce((n, r) => n + (r.items ? r.items.length : 0), 0);
  const totalAccepted = detailed.reduce((n, r) => n + (r.accepted_count || 0), 0);
  badge.textContent = totalItems ? '(' + totalItems + ' items, ' + totalAccepted + ' accepted)' : '';

  const fmtSec = s => {
    const m = Math.floor(s / 60);
    return m + ':' + String(Math.floor(s % 60)).padStart(2, '0');
  };

  let html = '';
  for (const run of detailed) {
    const items = run.items || [];
    const created = run.created_at ? new Date(run.created_at).toLocaleString() : '';
    html += '<div style="border:1px solid ' + cssVar('--border') + ';border-radius:6px;padding:8px;margin-bottom:8px">';
    html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">';
    html += '<div style="font-size:.78rem;color:' + cssVar('--accent') + ';font-weight:600">'
      + esc(run.method) + ' &middot; ' + items.length + ' items'
      + '<span style="color:' + cssVar('--text-secondary') + ';font-weight:400;margin-left:6px">' + esc(created) + '</span>'
      + '</div>';
    html += '<button onclick="deleteTuningRun(' + run.id + ')" style="background:none;border:none;color:' + cssVar('--danger') + ';cursor:pointer;font-size:.72rem" title="Delete run">&#10005;</button>';
    html += '</div>';

    if (!items.length) {
      html += '<span style="color:' + cssVar('--text-secondary') + ';font-size:.78rem">No items extracted</span>';
    } else {
      html += '<table class="maneuver-table"><thead><tr>';
      html += '<th>Parameter</th><th>Value</th><th>Segment</th><th>Conf</th><th>Status</th><th></th>';
      html += '</tr></thead><tbody>';
      for (const item of items) {
        const statusCls = 'te-status-' + item.status;
        const statusLabel = item.status.charAt(0).toUpperCase() + item.status.slice(1);
        html += '<tr>';
        html += '<td style="font-weight:600;color:' + cssVar('--text-primary') + '">' + esc(item.parameter_name) + '</td>';
        html += '<td style="color:' + cssVar('--accent') + ';font-variant-numeric:tabular-nums">' + item.extracted_value + '</td>';
        html += '<td><span class="te-segment-text" title="' + esc(item.segment_text) + '">'
          + esc(item.segment_text.length > 60 ? item.segment_text.slice(0, 60) + '\u2026' : item.segment_text)
          + '</span>'
          + '<span style="color:' + cssVar('--text-secondary') + ';font-size:.68rem">[' + fmtSec(item.segment_start) + ' \u2013 ' + fmtSec(item.segment_end) + ']</span>'
          + '</td>';
        html += '<td style="color:' + cssVar('--text-secondary') + '">' + (item.confidence * 100).toFixed(0) + '%</td>';
        html += '<td><span class="' + statusCls + '">' + statusLabel + '</span></td>';
        html += '<td style="white-space:nowrap">';
        if (item.status === 'pending') {
          html += '<button onclick="acceptTuningItem(' + item.id + ')" class="te-play-btn" title="Accept" style="color:' + cssVar('--success') + '">&#10003;</button>';
          html += '<button onclick="dismissTuningItem(' + item.id + ')" class="te-play-btn" title="Dismiss" style="color:' + cssVar('--text-muted') + '">&#10007;</button>';
        }
        if (_session.audio_session_id && !(item.segment_start === 0 && item.segment_end === 0)) {
          html += '<button onclick="playSegmentAudio(' + item.segment_start + ',' + item.segment_end + ')" class="te-play-btn" title="Play segment">&#9654;</button>';
        }
        html += '</td>';
        html += '</tr>';
      }
      html += '</tbody></table>';
    }
    html += '</div>';
  }
  body.innerHTML = html;
}

async function extractTuning() {
  if (!_transcriptId) { alert('No transcript available for extraction'); return; }
  const btn = document.getElementById('extract-tuning-btn');
  if (btn) { btn.textContent = '\u23F3'; btn.disabled = true; }
  try {
    const r = await fetch('/api/tuning/extract/' + _transcriptId, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({method: 'regex'}),
    });
    if (!r.ok) { alert('Extraction failed: ' + r.status); return; }
    await loadTuningExtractions();
  } finally {
    if (btn) { btn.innerHTML = '&#8635; Extract'; btn.disabled = false; }
  }
}

async function acceptTuningItem(itemId) {
  const r = await fetch('/api/tuning/items/' + itemId + '/accept', {method: 'POST'});
  if (!r.ok) { alert('Failed to accept item'); return; }
  await loadTuningExtractions();
  await loadBoatSettings();
}

async function dismissTuningItem(itemId) {
  const r = await fetch('/api/tuning/items/' + itemId + '/dismiss', {method: 'POST'});
  if (!r.ok) { alert('Failed to dismiss item'); return; }
  await loadTuningExtractions();
  await loadBoatSettings();
}

async function deleteTuningRun(runId) {
  if (!confirm('Delete this extraction run and all its items?')) return;
  const r = await fetch('/api/tuning/runs/' + runId, {method: 'DELETE'});
  if (!r.ok) { alert('Failed to delete run'); return; }
  await loadTuningExtractions();
}

function playSegmentAudio(start, end) {
  if (!_session.audio_session_id) return;
  if (!_tuningSegmentAudio) {
    _tuningSegmentAudio = document.createElement('audio');
    _tuningSegmentAudio.src = '/api/audio/' + _session.audio_session_id + '/stream';
    _tuningSegmentAudio.preload = 'auto';
  }
  const audio = _tuningSegmentAudio;
  // Clear any previous stop timer
  if (_tuningSegmentTimer) {
    audio.removeEventListener('timeupdate', _tuningSegmentTimer);
    _tuningSegmentTimer = null;
  }
  audio.currentTime = start;
  audio.play();
  _tuningSegmentTimer = function() {
    if (audio.currentTime >= end) {
      audio.pause();
      audio.removeEventListener('timeupdate', _tuningSegmentTimer);
      _tuningSegmentTimer = null;
    }
  };
  audio.addEventListener('timeupdate', _tuningSegmentTimer);
}

// ---------------------------------------------------------------------------
// Go
// ---------------------------------------------------------------------------

init();
