/* history.js — Session History page logic */

let currentType = '';
let currentOffset = 0;
const LIMIT = 25;
let loadTimer = null;

function setType(btn, t) {
  currentType = t;
  currentOffset = 0;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  load();
}

function scheduleLoad() {
  clearTimeout(loadTimer);
  loadTimer = setTimeout(load, 300);
}

async function load() {
  const params = new URLSearchParams();
  const q = document.getElementById('q').value.trim();
  if (q) params.set('q', q);
  if (currentType) params.set('type', currentType);
  const from = document.getElementById('from-date').value;
  const to = document.getElementById('to-date').value;
  if (from) params.set('from_date', from);
  if (to) params.set('to_date', to);
  params.set('limit', LIMIT);
  params.set('offset', currentOffset);
  const r = await fetch('/api/sessions?' + params);
  const data = await r.json();
  render(data);
}

function render(data) {
  const el = document.getElementById('results');
  if (!data.sessions.length) {
    el.innerHTML = '<div class="empty">No sessions found</div>';
    document.getElementById('pager').innerHTML = '';
    return;
  }
  el.innerHTML = data.sessions.map(s => {
    const start = fmtTimeShort(s.start_utc);
    const end = s.end_utc ? fmtTimeShort(s.end_utc) : 'in progress';
    const dur = (s.end_utc && s.duration_s != null) ? ' (' + fmtDuration(Math.round(s.duration_s)) + ')' : '';
    const parent = s.parent_race_name ? '<div class="session-meta">Debrief of ' + s.parent_race_name + '</div>' : '';
    const displayName = s.shared_name || s.name;
    const nameLink = '<a href="/session/' + s.id + '" style="color:inherit;text-decoration:none">' + displayName + '</a>';
    const localNameHint = s.shared_name ? '<div style="font-size:.72rem;color:var(--text-secondary);margin-top:1px">Local: ' + s.name + '</div>' : '';
    return '<div class="card"><div class="session-name">' + nameLink + '</div>'
      + '<div class="session-meta">' + s.date + ' &nbsp;·&nbsp; ' + start + ' → ' + end + dur + '</div>'
      + localNameHint
      + parent
      + '</div>';
  }).join('');

  const total = data.total;
  const page = Math.floor(currentOffset / LIMIT);
  const totalPages = Math.ceil(total / LIMIT);
  const pager = document.getElementById('pager');
  if (totalPages <= 1) {
    pager.innerHTML = '<span class="pager-info">' + total + ' session' + (total !== 1 ? 's' : '') + '</span>';
  } else {
    pager.innerHTML =
      '<button class="btn btn-secondary" style="padding:8px 14px" onclick="go(' + (page-1) + ')"' + (page===0?' disabled':'') + '>&#8592; Prev</button>'
      + '<span class="pager-info">Page ' + (page+1) + ' of ' + totalPages + ' (' + total + ' total)</span>'
      + '<button class="btn btn-secondary" style="padding:8px 14px" onclick="go(' + (page+1) + ')"' + (page>=totalPages-1?' disabled':'') + '>Next &#8594;</button>';
  }
}

function go(page) {
  currentOffset = page * LIMIT;
  load();
  window.scrollTo(0, 0);
}
