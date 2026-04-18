// Tag-picker custom element (#587 / #588 slice 3).
//
//   <tag-picker entity-type="bookmark" entity-id="42"></tag-picker>
//
// Loads the tag list from GET /api/tags?order_by=usage and the current
// attachments from GET /api/entities/{type}/{id}/tags. User can:
// - Type to filter (prefix/substring match on name)
// - Arrow up/down to navigate; Enter to attach the highlighted tag
// - Hit Enter with no match to inline-create a new tag and attach it in
//   one POST (server accepts {name: "..."})
// - Click a chip's × to detach
//
// Emits `change` events with detail = current attachment list so hosts
// (e.g. thread compose form) can re-render the badge strip.

class TagPicker extends HTMLElement {
  static get observedAttributes() { return ['entity-type', 'entity-id']; }

  constructor() {
    super();
    this._allTags = [];
    this._attached = []; // [{id, name, color}]
    this._filtered = [];
    this._selectedIdx = 0;
  }

  connectedCallback() { this._render(); this._wire(); this._refresh(); }
  attributeChangedCallback() { if (this._inputEl) this._refresh(); }

  get entityType() { return this.getAttribute('entity-type'); }
  get entityId() { return this.getAttribute('entity-id'); }

  async _refresh() {
    try {
      const [allResp, attachedResp] = await Promise.all([
        fetch('/api/tags?order_by=usage'),
        this.entityType && this.entityId
          ? fetch(`/api/entities/${this.entityType}/${this.entityId}/tags`)
          : Promise.resolve({ok: false}),
      ]);
      if (allResp.ok) this._allTags = await allResp.json();
      if (attachedResp.ok) this._attached = await attachedResp.json();
    } catch { /* non-fatal */ }
    this._renderChips();
    this._onInput();
  }

  _render() {
    this.style.display = 'block';
    this.innerHTML = `
      <div class="tag-picker-chips" style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:4px"></div>
      <div style="display:flex;gap:6px;align-items:center">
        <input class="tag-picker-input" type="text" placeholder="Add tag\u2026"
               autocomplete="off" style="flex:1" />
      </div>
      <div class="tag-picker-list" role="listbox"
           style="display:none;max-height:200px;overflow:auto;border:1px solid var(--border);border-radius:4px;margin-top:4px;background:var(--bg-primary)"></div>
    `;
    this._chipsEl = this.querySelector('.tag-picker-chips');
    this._inputEl = this.querySelector('.tag-picker-input');
    this._listEl = this.querySelector('.tag-picker-list');
  }

  _wire() {
    this._inputEl.addEventListener('input', () => this._onInput());
    this._inputEl.addEventListener('focus', () => this._show());
    this._inputEl.addEventListener('keydown', (ev) => this._onKey(ev));
    this._inputEl.addEventListener('blur', () => setTimeout(() => this._hide(), 150));
    this._listEl.addEventListener('mousedown', (ev) => {
      const item = ev.target.closest('[data-idx]');
      if (!item) return;
      ev.preventDefault();
      this._attach(this._filtered[parseInt(item.dataset.idx, 10)]);
    });
  }

  _attachedIds() { return new Set(this._attached.map(t => t.id)); }

  _onInput() {
    const q = this._inputEl.value.trim().toLowerCase();
    const attached = this._attachedIds();
    const unattached = this._allTags.filter(t => !attached.has(t.id));
    this._filtered = q
      ? unattached.filter(t => t.name.includes(q))
      : unattached;
    this._selectedIdx = this._filtered.length ? 0 : -1;
    this._renderList(q);
  }

  _onKey(ev) {
    if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      if (!this._filtered.length) return;
      this._selectedIdx = Math.min(this._filtered.length - 1, this._selectedIdx + 1);
      this._renderList(this._inputEl.value.trim().toLowerCase());
    } else if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      this._selectedIdx = Math.max(0, this._selectedIdx - 1);
      this._renderList(this._inputEl.value.trim().toLowerCase());
    } else if (ev.key === 'Enter') {
      ev.preventDefault();
      const q = this._inputEl.value.trim().toLowerCase();
      if (this._selectedIdx >= 0 && this._filtered[this._selectedIdx]) {
        this._attach(this._filtered[this._selectedIdx]);
      } else if (q) {
        this._attach({name: q}); // inline-create via {name} payload
      }
    } else if (ev.key === 'Escape') {
      this._hide();
      this._inputEl.blur();
    }
  }

  async _attach(tagOrName) {
    if (!this.entityType || !this.entityId) return;
    const body = tagOrName.id ? {tag_id: tagOrName.id} : {name: tagOrName.name};
    try {
      const r = await fetch(`/api/entities/${this.entityType}/${this.entityId}/tags`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      if (!r.ok) return;
    } catch { return; }
    this._inputEl.value = '';
    await this._refresh();
    this._emit();
  }

  async _detach(tagId) {
    if (!this.entityType || !this.entityId) return;
    try {
      const r = await fetch(
        `/api/entities/${this.entityType}/${this.entityId}/tags/${tagId}`,
        {method: 'DELETE'}
      );
      if (!r.ok && r.status !== 204) return;
    } catch { return; }
    await this._refresh();
    this._emit();
  }

  _renderChips() {
    if (!this._attached.length) {
      this._chipsEl.innerHTML = '<span style="font-size:.72rem;color:var(--text-secondary)">No tags</span>';
      return;
    }
    this._chipsEl.innerHTML = this._attached.map(t => {
      const color = t.color || 'var(--accent)';
      const name = (t.name || '').replace(/</g, '&lt;');
      return `<span class="tp-chip" data-tag-id="${t.id}" style="display:inline-flex;align-items:center;gap:4px;padding:1px 6px 1px 8px;border-radius:10px;font-size:.72rem;background:var(--bg-secondary);border:1px solid ${color}">${name}<button type="button" data-detach="${t.id}" style="background:none;border:none;color:var(--text-secondary);cursor:pointer;padding:0 2px">&times;</button></span>`;
    }).join('');
    this._chipsEl.querySelectorAll('[data-detach]').forEach(btn => {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        this._detach(parseInt(btn.dataset.detach, 10));
      });
    });
  }

  _renderList(q) {
    if (!this._filtered.length) {
      if (q) {
        this._listEl.innerHTML = `<div style="padding:6px 8px;font-size:.78rem;color:var(--accent);cursor:pointer" data-inline-create>Create tag &ldquo;${q.replace(/</g, '&lt;')}&rdquo; (Enter)</div>`;
      } else {
        this._listEl.innerHTML = '<div style="padding:6px 8px;color:var(--text-secondary);font-size:.78rem">No more tags to add</div>';
      }
      return;
    }
    this._listEl.innerHTML = this._filtered.map((t, idx) => {
      const selected = idx === this._selectedIdx;
      const bg = selected ? 'background:var(--bg-secondary);' : '';
      const count = t.usage_count ? ` <span style="color:var(--text-secondary);font-size:.7rem">(${t.usage_count})</span>` : '';
      const name = (t.name || '').replace(/</g, '&lt;');
      return `<div role="option" data-idx="${idx}" style="padding:6px 8px;cursor:pointer;font-size:.82rem;${bg}">${name}${count}</div>`;
    }).join('');
  }

  _show() { this._listEl.style.display = ''; }
  _hide() { this._listEl.style.display = 'none'; }

  _emit() {
    this.dispatchEvent(new CustomEvent('change', {
      detail: {attached: this._attached.slice()}, bubbles: true,
    }));
  }
}

if (!customElements.get('tag-picker')) {
  customElements.define('tag-picker', TagPicker);
}
