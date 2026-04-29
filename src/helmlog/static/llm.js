// LLM transcript Q&A and callback panel for the session view (#697).
//
// Mounted on #llm-card when the page loads. Hides itself if the race has
// no transcript yet (HTTP 404 on the cost endpoint is treated as "show
// later"). Polls history once on mount; subsequent updates happen on
// user actions (ask/save/run).

(function () {
  'use strict';

  const card = document.getElementById('llm-card');
  if (!card) return;
  const raceId = card.dataset.raceId;
  if (!raceId) return;

  const els = {
    costHeader: document.getElementById('llm-cost-header'),
    consentBanner: document.getElementById('llm-consent-banner'),
    consentAck: document.getElementById('llm-consent-ack'),
    qaForm: document.getElementById('llm-qa-form'),
    qaInput: document.getElementById('llm-qa-input'),
    qaHistory: document.getElementById('llm-qa-history'),
    callbacksList: document.getElementById('llm-callbacks-list'),
    callbacksRerun: document.getElementById('llm-callbacks-rerun'),
    qaPane: document.getElementById('llm-qa-pane'),
    callbacksPane: document.getElementById('llm-callbacks-pane'),
    tabs: card.querySelectorAll('.llm-tab'),
  };

  let consented = false;
  let isAdmin = false;

  function fmtCost(usd) {
    return '$' + (usd || 0).toFixed(3);
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
  }

  // Render an answer with citations linkified as [HH:MM:SS] chips that
  // seek the audio player on click. Falls back to plain text when no
  // audio player is present.
  function renderAnswer(text) {
    return escapeHtml(text).replace(
      /\[(\d{1,2}:\d{2}:\d{2})\]/g,
      (_, ts) => `<a href="#" data-ts="${ts}" class="llm-cite" style="color:var(--accent);text-decoration:none;border-bottom:1px dotted var(--accent)">[${ts}]</a>`,
    );
  }

  function bindCitationSeek(root) {
    root.querySelectorAll('.llm-cite').forEach((a) => {
      a.addEventListener('click', (ev) => {
        ev.preventDefault();
        const ts = a.dataset.ts;
        const audio = document.querySelector('audio');
        if (!audio || !ts) return;
        const [h, m, s] = ts.split(':').map(Number);
        const seconds = h * 3600 + m * 60 + s;
        const offset = seconds - (window.HELMLOG_AUDIO_START_OFFSET || 0);
        if (offset >= 0) audio.currentTime = offset;
        audio.play().catch(() => {});
      });
    });
  }

  async function loadCost() {
    const resp = await fetch(`/api/sessions/${raceId}/llm/cost`);
    if (!resp.ok) return null;
    return resp.json();
  }

  async function loadConsent() {
    const resp = await fetch('/api/llm/consent');
    if (!resp.ok) return { acknowledged: false };
    return resp.json();
  }

  async function loadHistory() {
    const resp = await fetch(`/api/sessions/${raceId}/llm/qa`);
    if (!resp.ok) return { qa: [] };
    return resp.json();
  }

  async function loadCallbacks() {
    const resp = await fetch(`/api/sessions/${raceId}/llm/callbacks`);
    if (!resp.ok) return { callbacks: [], job: null };
    return resp.json();
  }

  function renderHistory(items) {
    if (!items.length) {
      els.qaHistory.innerHTML = '<div style="color:var(--text-secondary);font-size:.8rem;font-style:italic">No questions yet.</div>';
      return;
    }
    els.qaHistory.innerHTML = items.map((qa) => `
      <div class="llm-qa-row" data-qa-id="${qa.id}" style="margin-bottom:10px;padding:8px;border:1px solid var(--border);border-radius:4px">
        <div style="font-weight:500;font-size:.85rem">Q: ${escapeHtml(qa.question)}</div>
        <div style="margin-top:4px;font-size:.85rem">${qa.answer ? renderAnswer(qa.answer) : '<span style="color:var(--text-secondary)">(failed: ' + escapeHtml(qa.error_msg || 'unknown') + ')</span>'}</div>
        <div style="margin-top:6px;display:flex;gap:8px;align-items:center;font-size:.7rem;color:var(--text-secondary)">
          <span>${fmtCost(qa.cost_usd)} · ${qa.input_tokens || 0}+${qa.output_tokens || 0} tok</span>
          ${qa.answer ? `<button class="btn-save-moment" data-qa-id="${qa.id}" style="background:none;border:1px solid var(--border);color:var(--text-secondary);border-radius:3px;padding:2px 8px;font-size:.7rem;cursor:pointer">Save as moment</button>` : ''}
        </div>
      </div>
    `).join('');
    bindCitationSeek(els.qaHistory);
    els.qaHistory.querySelectorAll('.btn-save-moment').forEach((btn) => {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        const r = await fetch(`/api/llm/qa/${btn.dataset.qaId}/save-as-moment`, { method: 'POST' });
        btn.textContent = r.ok ? 'Saved ✓' : 'Failed';
      });
    });
  }

  function renderCallbacks(data) {
    const cbs = data.callbacks || [];
    const job = data.job;
    if (!cbs.length) {
      els.callbacksList.innerHTML = `<div style="color:var(--text-secondary);font-style:italic">${job && job.status === 'Complete' ? 'No callbacks detected.' : 'Not run yet.'}</div>`;
      return;
    }
    // Group by speaker
    const bySpeaker = {};
    cbs.forEach((cb) => {
      const k = cb.speaker_label || '(unknown)';
      (bySpeaker[k] = bySpeaker[k] || []).push(cb);
    });
    els.callbacksList.innerHTML = Object.entries(bySpeaker).map(([speaker, list]) => `
      <div style="margin-bottom:10px">
        <div style="font-weight:500;margin-bottom:4px">${escapeHtml(speaker)}</div>
        ${list.map((cb) => `
          <div style="margin-left:10px;padding:6px 8px;border-left:2px solid var(--accent);margin-bottom:4px">
            <a href="#" data-ts="${cb.anchor_ts.split('T').pop().slice(0, 8)}" class="llm-cite" style="color:var(--accent);font-size:.7rem">${escapeHtml(cb.anchor_ts.split('T').pop().slice(0, 8))}</a>
            <span style="margin-left:8px">${escapeHtml(cb.source_excerpt)}</span>
            <button class="btn-cb-save-moment" data-cb-id="${cb.id}" style="margin-left:8px;background:none;border:1px solid var(--border);color:var(--text-secondary);border-radius:3px;padding:1px 6px;font-size:.65rem;cursor:pointer">Save as moment</button>
            ${cb.rationale ? `<div style="font-size:.7rem;color:var(--text-secondary);margin-top:2px">${escapeHtml(cb.rationale)}</div>` : ''}
          </div>
        `).join('')}
      </div>
    `).join('');
    bindCitationSeek(els.callbacksList);
    els.callbacksList.querySelectorAll('.btn-cb-save-moment').forEach((btn) => {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        const r = await fetch(`/api/llm/callbacks/${btn.dataset.cbId}/save-as-moment`, { method: 'POST' });
        btn.textContent = r.ok ? 'Saved ✓' : 'Failed';
      });
    });
  }

  function setCostHeader(cost) {
    if (!cost) {
      els.costHeader.textContent = '';
      return;
    }
    const stateLabel = cost.state === 'AtCap' ? '⛔ at cap' :
      cost.state === 'SoftWarned' ? '⚠ over soft warn' : '';
    els.costHeader.textContent = `${fmtCost(cost.current_spend_usd)} / ${fmtCost(cost.hard_cap_usd)} ${stateLabel}`;
  }

  async function refresh() {
    const [cost, consent, history, callbacks] = await Promise.all([
      loadCost(), loadConsent(), loadHistory(), loadCallbacks(),
    ]);
    setCostHeader(cost);
    consented = consent.acknowledged;
    if (!consented) {
      els.consentBanner.style.display = 'block';
    } else {
      els.consentBanner.style.display = 'none';
    }
    // Best-effort admin detection: try the admin-only endpoint silently.
    isAdmin = (await fetch('/api/me').then(r => r.json()).catch(() => null) || {}).role === 'admin';
    if (isAdmin && consented) els.callbacksRerun.style.display = '';
    renderHistory(history.qa || []);
    renderCallbacks(callbacks);
    card.style.display = '';
  }

  els.consentAck.addEventListener('click', async () => {
    els.consentAck.disabled = true;
    els.consentAck.textContent = 'Acknowledging…';
    try {
      const r = await fetch('/api/llm/consent', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' },
      });
      const text = await r.text();
      if (!r.ok) {
        console.error('LLM consent POST failed', r.status, text);
        alert(`Consent failed (${r.status}): ${text.slice(0, 200)}`);
        els.consentAck.disabled = false;
        els.consentAck.textContent = 'Acknowledge & enable';
        return;
      }
      console.log('LLM consent POST ok', text);
      await refresh();
    } catch (err) {
      console.error('LLM consent click handler threw', err);
      alert('Consent click failed: ' + err.message);
      els.consentAck.disabled = false;
      els.consentAck.textContent = 'Acknowledge & enable';
    }
  });

  els.qaForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const q = els.qaInput.value.trim();
    if (!q) return;
    els.qaInput.disabled = true;
    try {
      const resp = await fetch(`/api/sessions/${raceId}/llm/qa`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q }),
      });
      if (resp.status === 409) {
        const j = await resp.json();
        if (j.reason === 'confirmation_required') {
          if (confirm(`Spend so far: ${fmtCost(j.current_spend_usd)}. Continue?`)) {
            const r2 = await fetch(`/api/sessions/${raceId}/llm/qa`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ question: q, confirm_cost: true }),
            });
            if (!r2.ok) alert(`Failed: ${(await r2.json()).reason || r2.status}`);
          }
        } else {
          alert(`Blocked: ${j.reason}`);
        }
      } else if (resp.status === 429) {
        const j = await resp.json();
        alert(`Cost cap: ${j.reason}`);
      } else if (!resp.ok) {
        alert(`Error: ${resp.status}`);
      }
      els.qaInput.value = '';
      await refresh();
    } finally {
      els.qaInput.disabled = false;
      els.qaInput.focus();
    }
  });

  els.callbacksRerun.addEventListener('click', async () => {
    els.callbacksRerun.disabled = true;
    try {
      const r = await fetch(`/api/sessions/${raceId}/llm/callbacks/run`, { method: 'POST' });
      if (!r.ok) alert(`Re-run failed: ${r.status}`);
      await refresh();
    } finally {
      els.callbacksRerun.disabled = false;
    }
  });

  els.tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      const which = tab.dataset.tab;
      els.tabs.forEach((t) => {
        t.style.color = t === tab ? 'var(--accent)' : 'var(--text-secondary)';
        t.style.borderBottomColor = t === tab ? 'var(--accent)' : 'transparent';
      });
      els.qaPane.style.display = which === 'qa' ? '' : 'none';
      els.callbacksPane.style.display = which === 'callbacks' ? '' : 'none';
    });
  });

  // Mount on DOMContentLoaded so audio-player and friends are wired first.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', refresh);
  } else {
    refresh();
  }
})();
