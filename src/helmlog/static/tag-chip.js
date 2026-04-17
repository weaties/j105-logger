// Read-only tag chip custom element (#588 slice 1 stub, wired up in slice 3 / #587).
//
// Usage: <tag-chip name="weather mark" color="#3b82f6"></tag-chip>
//
// Inert by design — no click handlers, no remove button. Slice 3 replaces
// this with a full interactive component using the same tag name.

class TagChip extends HTMLElement {
  static get observedAttributes() { return ['name', 'color']; }

  connectedCallback() { this._render(); }
  attributeChangedCallback() { this._render(); }

  _render() {
    const name = this.getAttribute('name') || '';
    const color = this.getAttribute('color') || 'var(--accent)';
    this.style.display = 'inline-flex';
    this.style.alignItems = 'center';
    this.style.padding = '1px 7px';
    this.style.borderRadius = '10px';
    this.style.fontSize = '.72rem';
    this.style.background = 'var(--bg-secondary)';
    this.style.border = `1px solid ${color}`;
    this.style.color = 'var(--text-primary)';
    this.style.whiteSpace = 'nowrap';
    this.textContent = name;
  }
}

if (!customElements.get('tag-chip')) {
  customElements.define('tag-chip', TagChip);
}
