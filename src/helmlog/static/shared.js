/* shared.js — common utilities loaded by all pages */

// ---------------------------------------------------------------------------
// CSS variable reader — theme-aware color helper
// ---------------------------------------------------------------------------

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// ---------------------------------------------------------------------------
// Time formatting
// ---------------------------------------------------------------------------

let _tz = 'UTC';

function fmtDuration(s) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = Math.floor(s % 60);
  if (h) return h + ':' + String(m).padStart(2, '0') + ':' + String(ss).padStart(2, '0');
  return m + ':' + String(ss).padStart(2, '0');
}

function fmtTime(iso) {
  if (!iso) return '\u2014';
  try {
    return new Date(iso).toLocaleTimeString('en-US', {
      timeZone: _tz, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
    });
  } catch (e) {
    return new Date(iso).toISOString().substring(11, 19) + ' UTC';
  }
}

function fmtTimeShort(iso) {
  if (!iso) return '\u2014';
  try {
    return new Date(iso).toLocaleTimeString('en-US', {
      timeZone: _tz, hour: '2-digit', minute: '2-digit', hour12: false
    });
  } catch (e) {
    return new Date(iso).toISOString().substring(11, 16) + ' UTC';
  }
}

// ---------------------------------------------------------------------------
// HTML escaping
// ---------------------------------------------------------------------------

function esc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ---------------------------------------------------------------------------
// Nav bar — hamburger toggle, admin link reveal, profile
// ---------------------------------------------------------------------------

function toggleNav() {
  const links = document.getElementById('nav-links');
  const btn = document.getElementById('nav-hamburger');
  if (!links || !btn) return;
  const open = links.classList.toggle('open');
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

// Close nav when a link inside it is activated (mobile UX)
document.addEventListener('DOMContentLoaded', function () {
  const links = document.getElementById('nav-links');
  if (links) {
    links.addEventListener('click', function (e) {
      if (e.target.tagName === 'A') {
        links.classList.remove('open');
        const btn = document.getElementById('nav-hamburger');
        if (btn) btn.setAttribute('aria-expanded', 'false');
      }
    });
  }

  // Close nav when clicking outside
  document.addEventListener('click', function (e) {
    const nav = document.getElementById('site-nav');
    if (nav && !nav.contains(e.target)) {
      const links = document.getElementById('nav-links');
      const btn = document.getElementById('nav-hamburger');
      if (links) links.classList.remove('open');
      if (btn) btn.setAttribute('aria-expanded', 'false');
    }
  });

  // Keyboard: close on Escape
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      const links = document.getElementById('nav-links');
      const btn = document.getElementById('nav-hamburger');
      if (links) links.classList.remove('open');
      if (btn) {
        btn.setAttribute('aria-expanded', 'false');
        btn.focus();
      }
    }
  });
});

let _isDeveloper = false;
let _userRole = 'viewer';

function initNav() {
  fetch('/api/me').then(r => r.json()).then(u => {
    _isDeveloper = !!u.is_developer;
    _userRole = u.role || 'viewer';
    if (u.role === 'admin') {
      document.querySelectorAll('.admin-link').forEach(el => el.style.setProperty('display', 'inline', 'important'));
    }
    if (u.id) {
      const p = document.getElementById('nav-profile');
      if (p) {
        p.style.setProperty('display', 'inline', 'important');
        document.getElementById('nav-avatar').src = '/avatars/' + u.id + '.jpg';
        document.getElementById('nav-profile-name').textContent = u.name || 'Profile';
      }
    }
  }).catch(() => {});
}

// ---------------------------------------------------------------------------
// Timezone initialization
// ---------------------------------------------------------------------------

function initTimezone() {
  return fetch('/api/state').then(r => r.json()).then(s => {
    if (s.timezone) _tz = s.timezone;
    return s;
  }).catch(() => null);
}

// ---------------------------------------------------------------------------
// Video position parsing
// ---------------------------------------------------------------------------

function parseVideoPosition(str) {
  str = str.trim();
  const parts = str.split(':').map(Number);
  if (parts.some(isNaN)) return null;
  if (parts.length === 1) return parts[0];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  return null;
}

// ---------------------------------------------------------------------------
// Video selector button labels
// ---------------------------------------------------------------------------

// Given a list of video objects (each with .title, .label, .video_id), return
// a parallel list of short strings to use as switcher-button labels. The
// algorithm strips time-of-day tokens from each title, then collapses
// everything that's common across all titles (longest common prefix +
// longest common suffix) so only the part that actually varies is left.
//
// When that leaves nothing unique (e.g. grandfathered videos that only differ
// by upload time), fall back to the stripped time tokens themselves. Final
// fallback is "Video 1/2/3" so a degenerate set never renders empty buttons.
//
// Pure function — no DOM access. Called by session.js and history.js.
function videoButtonLabels(videos) {
  // HH:MM or HH:MM:SS, optionally followed by a 2–5 char uppercase TZ abbr.
  const TIME_RE = /\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*[A-Z]{2,5})?\b/g;

  const sources = videos.map((v, i) => v.title || v.label || ('Video ' + (i + 1)));
  const stripped = sources.map(s => s.replace(TIME_RE, '').replace(/\s+/g, ' ').trim());
  const times = sources.map(s => {
    const m = s.match(TIME_RE);
    return m ? m.join(' ') : '';
  });

  function lcp(strs) {
    if (!strs.length) return '';
    let p = strs[0];
    for (let i = 1; i < strs.length; i++) {
      while (p && !strs[i].startsWith(p)) p = p.substring(0, p.length - 1);
      if (!p) return '';
    }
    return p;
  }
  function lcsuf(strs) {
    if (!strs.length) return '';
    let s = strs[0];
    for (let i = 1; i < strs.length; i++) {
      while (s && !strs[i].endsWith(s)) s = s.substring(1);
      if (!s) return '';
    }
    return s;
  }

  const prefix = lcp(stripped);
  const afterPrefix = stripped.map(s => s.substring(prefix.length));
  const suffix = lcsuf(afterPrefix);
  const unique = afterPrefix.map(s => s.substring(0, s.length - suffix.length).trim());

  const collapsed = unique.every(l => !l) || new Set(unique).size === 1;
  if (collapsed) {
    if (new Set(times).size > 1 && times.every(t => t)) return times;
    return videos.map((_, i) => 'Video ' + (i + 1));
  }
  return unique.map((l, i) => l || ('Video ' + (i + 1)));
}

// ---------------------------------------------------------------------------
// Grafana URL helpers
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// GitHub issue URL builder (in-app bug reports / feature requests)
// ---------------------------------------------------------------------------

function buildIssueUrl(kind) {
  var version = '';
  var meta = document.querySelector('meta[name="helmlog-version"]');
  if (meta) version = meta.getAttribute('content') || '';

  var page = location.pathname + location.search;
  var screen = window.innerWidth + '×' + window.innerHeight;
  var ua = navigator.userAgent;
  var ts = new Date().toISOString();

  // Extract session/race ID from URL if applicable
  var idMatch = page.match(/\/(session|race)\/(\d+)/);
  var idLine = idMatch ? '| ' + idMatch[1] + '_id | `' + idMatch[2] + '` |\n' : '';

  var isBug = kind === 'bug';
  var title = isBug ? '[Bug] ' : '[Feature] ';
  var labels = isBug ? 'from-app,bug' : 'from-app,enhancement';

  var body = isBug
    ? '## Description\n\n<!-- Describe the bug -->\n\n'
      + '## Steps to reproduce\n\n1. \n2. \n3. \n\n'
      + '## Expected vs actual behavior\n\n<!-- What did you expect? What happened instead? -->\n\n'
    : '## Description\n\n<!-- Describe the feature you\'d like -->\n\n'
      + '## Use case\n\n<!-- Why is this needed? -->\n\n';

  body += '---\n\n*Submitted from HelmLog UI*\n\n'
    + '| | |\n|---|---|\n'
    + '| Page | `' + page + '` |\n'
    + '| Version | `' + version + '` |\n'
    + '| Browser | `' + ua + '` |\n'
    + '| Screen | `' + screen + '` |\n'
    + idLine
    + '| Time | `' + ts + '` |\n';

  return 'https://github.com/weaties/helmlog/issues/new'
    + '?title=' + encodeURIComponent(title)
    + '&body=' + encodeURIComponent(body)
    + '&labels=' + encodeURIComponent(labels);
}

// ---------------------------------------------------------------------------
// Grafana URL helpers
// ---------------------------------------------------------------------------

function initGrafana(grafanaPort, grafanaUid, skPort) {
  const isDefaultPort = !location.port || location.port === '443' || location.port === '80';
  window.GRAFANA_BASE = isDefaultPort
    ? location.origin + '/grafana'
    : location.protocol + '//' + location.hostname + ':' + grafanaPort;
  window.GRAFANA_UID = grafanaUid;
  if (skPort) {
    window.SK_BASE = isDefaultPort
      ? location.origin + '/sk'
      : location.protocol + '//' + location.hostname + ':' + skPort;
  }
}
