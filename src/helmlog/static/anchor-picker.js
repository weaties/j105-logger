// Anchor-picker custom element (#478 / #588 slice 2).
//
// <anchor-picker session-id="123"></anchor-picker>
//
// Fetches pickable anchors from /api/sessions/{id}/anchors, renders a text
// input + filtered dropdown, and emits a `change` event when the user picks
// one:
//
//   picker.addEventListener('change', ev => {
//     ev.detail.anchor  // {kind, entity_id?, t_start?, t_end?} | null
//     ev.detail.label   // human-readable label for display (string | null)
//   });
//
// Keyboard: ArrowUp/Down navigate, Enter picks, Escape cancels. Typing
// filters by substring against the label. If the user presses Enter with no
// list item selected and the `fallbackToCursor` attribute is present (the
// default when composing a new thread), the picker emits an ad-hoc
// timestamp anchor at the current replay cursor.
//
// This is the reusable primitive — slice 3 (#587) will add a second mode
// for tag selection using the same keyboard model and rendering.

class AnchorPicker extends HTMLElement {
  static get observedAttributes() { return ['session-id']; }

  constructor() {
    super();
    this._anchors = [];
    this._filtered = [];
    this._selectedIdx = -1;
    this._selectedAnchor = null;
    this._fallbackCursor = null; // set by host (session.js) when composing
  }

  connectedCallback() {
    this._render();
    this._wire();
    this._refresh();
  }

  attributeChangedCallback(name) {
    if (name === 'session-id' && this._listEl) this._refresh();
  }

  get sessionId() { return this.getAttribute('session-id'); }
  get value() { return this._selectedAnchor; }

  set fallbackCursor(utc) { this._fallbackCursor = utc; }

  clear() {
    this._selectedAnchor = null;
    this._selectedIdx = -1;
    if (this._inputEl) this._inputEl.value = '';
    this._renderBadge(null);
    this._renderList();
  }

  _render() {
    this.style.display = 'block';
    this.innerHTML = `
      <div class="anchor-picker-row" style="display:flex;gap:6px;align-items:center">
        <input class="anchor-picker-input" type="text" placeholder="Search anchors\u2026"
               autocomplete="off" style="flex:1" />
        <span class="anchor-picker-badge" style="font-size:.72rem;color:var(--text-secondary)"></span>
        <button type="button" class="anchor-picker-clear"
                style="background:none;border:none;color:var(--text-secondary);cursor:pointer;font-size:.72rem;text-decoration:underline;display:none">clear</button>
      </div>
      <div class="anchor-picker-list" role="listbox"
           style="display:none;max-height:220px;overflow:auto;border:1px solid var(--border);border-radius:4px;margin-top:4px;background:var(--bg-primary)"></div>
    `;
    this._inputEl = this.querySelector('.anchor-picker-input');
    this._badgeEl = this.querySelector('.anchor-picker-badge');
    this._clearEl = this.querySelector('.anchor-picker-clear');
    this._listEl = this.querySelector('.anchor-picker-list');
  }

  _wire() {
    this._inputEl.addEventListener('input', () => this._onInput());
    this._inputEl.addEventListener('focus', () => this._showList());
    this._inputEl.addEventListener('keydown', (ev) => this._onKey(ev));
    this._inputEl.addEventListener('blur', () => setTimeout(() => this._hideList(), 150));
    this._clearEl.addEventListener('click', () => this._emit(null, null));
    this._listEl.addEventListener('mousedown', (ev) => {
      const item = ev.target.closest('[data-idx]');
      if (!item) return;
      ev.preventDefault();
      this._pick(parseInt(item.dataset.idx, 10));
    });
  }

  async _refresh() {
    if (!this.sessionId) { this._anchors = []; return; }
    try {
      const resp = await fetch(`/api/sessions/${this.sessionId}/anchors`);
      if (!resp.ok) return;
      this._anchors = await resp.json();
      this._filtered = this._anchors.slice();
      this._renderList();
    } catch { /* silent */ }
  }

  _onInput() {
    const q = this._inputEl.value.trim().toLowerCase();
    this._filtered = q
      ? this._anchors.filter(a => (a.label || '').toLowerCase().includes(q))
      : this._anchors.slice();
    this._selectedIdx = this._filtered.length ? 0 : -1;
    this._renderList();
    this._showList();
  }

  _onKey(ev) {
    if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      if (!this._filtered.length) return;
      this._selectedIdx = Math.min(this._filtered.length - 1, this._selectedIdx + 1);
      this._renderList();
    } else if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      this._selectedIdx = Math.max(0, this._selectedIdx - 1);
      this._renderList();
    } else if (ev.key === 'Enter') {
      ev.preventDefault();
      if (this._selectedIdx >= 0 && this._filtered[this._selectedIdx]) {
        this._pickAnchor(this._filtered[this._selectedIdx]);
      } else if (this._fallbackCursor) {
        // No selection — use current playhead as raw timestamp anchor
        this._pickAnchor({
          kind: 'timestamp',
          t_start: this._fallbackCursor,
          label: 'Current replay time',
        });
      }
    } else if (ev.key === 'Escape') {
      this._hideList();
      this._inputEl.blur();
    }
  }

  _pick(idx) {
    const a = this._filtered[idx];
    if (a) this._pickAnchor(a);
  }

  _pickAnchor(raw) {
    const anchor = this._normalize(raw);
    const label = raw.label || '';
    this._selectedAnchor = anchor;
    this._inputEl.value = label;
    this._renderBadge(label);
    this._hideList();
    this._emit(anchor, label);
  }

  _normalize(raw) {
    // The /api/sessions/{id}/anchors endpoint includes a `t_start` on every
    // row so the picker can render a time label, but the Anchor schema only
    // allows `t_start` on kind=timestamp/segment. Strip it for entity-ref
    // kinds or the server rejects the payload.
    const a = { kind: raw.kind };
    if (raw.kind === 'timestamp') {
      if (raw.t_start) a.t_start = raw.t_start;
    } else if (raw.kind === 'segment') {
      if (raw.t_start) a.t_start = raw.t_start;
      if (raw.t_end) a.t_end = raw.t_end;
    } else {
      // maneuver | bookmark | race | start — entity_id only
      if (raw.entity_id !== undefined && raw.entity_id !== null) {
        a.entity_id = raw.entity_id;
      }
    }
    return a;
  }

  _emit(anchor, label) {
    this._selectedAnchor = anchor;
    if (!anchor) {
      this._inputEl.value = '';
      this._renderBadge(null);
    }
    this.dispatchEvent(new CustomEvent('change', {
      detail: { anchor, label },
      bubbles: true,
    }));
  }

  _renderBadge(label) {
    if (label) {
      this._badgeEl.textContent = label.length > 40 ? label.slice(0, 37) + '\u2026' : label;
      this._badgeEl.style.color = 'var(--warning)';
      this._clearEl.style.display = '';
    } else {
      this._badgeEl.textContent = '';
      this._clearEl.style.display = 'none';
    }
  }

  _renderList() {
    if (!this._filtered.length) {
      this._listEl.innerHTML = '<div style="padding:6px 8px;color:var(--text-secondary);font-size:.78rem">No matches</div>';
      return;
    }
    this._listEl.innerHTML = this._filtered.map((a, idx) => {
      const selected = idx === this._selectedIdx;
      const bg = selected ? 'background:var(--bg-secondary);' : '';
      const kindBadge = `<span style="color:var(--text-secondary);font-size:.66rem;margin-right:6px">${a.kind}</span>`;
      const label = (a.label || '').replace(/</g, '&lt;');
      return `<div role="option" data-idx="${idx}" style="padding:6px 8px;cursor:pointer;font-size:.82rem;${bg}">${kindBadge}${label}</div>`;
    }).join('');
  }

  _showList() { this._listEl.style.display = ''; }
  _hideList() { this._listEl.style.display = 'none'; }
}

if (!customElements.get('anchor-picker')) {
  customElements.define('anchor-picker', AnchorPicker);
}
