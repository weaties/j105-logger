"""Color scheme presets and CSS variable generation for HelmLog.

Six built-in presets cover sunlight / low-light / branded use cases.
All presets meet WCAG AA contrast ratio (4.5:1) between text-primary and bg-primary.
Custom schemes can be created by admins and stored in SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    """One complete color scheme."""

    id: str
    name: str
    # Page / body
    bg_primary: str
    bg_secondary: str  # cards, panels
    # Text
    text_primary: str
    text_secondary: str  # muted labels, meta
    text_muted: str  # dimmer than secondary — disabled, zero-state
    # Accent — h1, instrument values, links
    accent: str
    # Primary action button
    action: str
    action_text: str  # text on top of action
    # Semantic status colors
    success: str  # green — positive feedback, active states
    danger: str  # red — errors, destructive actions
    warning: str  # amber — caution, synthesize, RC markers
    # Borders
    border_color: str  # nav, section separators
    border_input: str  # form inputs
    border_row: str  # table row dividers


BUILTIN_PRESETS: dict[str, Theme] = {
    "ocean_dark": Theme(
        id="ocean_dark",
        name="Ocean Dark",
        bg_primary="#0a1628",
        bg_secondary="#131f35",
        text_primary="#e8eaf0",
        text_secondary="#8892a4",
        text_muted="#6b7280",
        accent="#7eb8f7",
        action="#2563eb",
        action_text="#ffffff",
        success="#4ade80",
        danger="#f87171",
        warning="#fbbf24",
        border_color="#1e3a5f",
        border_input="#374151",
        border_row="#0d1a2e",
    ),
    "sunlight": Theme(
        id="sunlight",
        name="Sunlight High-Contrast",
        bg_primary="#ffffff",
        bg_secondary="#f0f4f8",
        text_primary="#000000",
        text_secondary="#555555",
        text_muted="#888888",
        accent="#0055aa",
        action="#0055aa",
        action_text="#ffffff",
        success="#007a2f",
        danger="#cc0000",
        warning="#b36b00",
        border_color="#b0c4d8",
        border_input="#0055aa",
        border_row="#d8e4f0",
    ),
    "racing_yellow": Theme(
        id="racing_yellow",
        name="Racing Yellow",
        bg_primary="#000000",
        bg_secondary="#111111",
        text_primary="#ffd600",
        text_secondary="#b8a000",
        text_muted="#807000",
        accent="#ffd600",
        action="#ffd600",
        action_text="#000000",
        success="#4ade80",
        danger="#ff6b6b",
        warning="#ffd600",
        border_color="#333300",
        border_input="#ffd600",
        border_row="#1a1a00",
    ),
    "sunset_red": Theme(
        id="sunset_red",
        name="Sunset Red",
        bg_primary="#1a0000",
        bg_secondary="#2a0808",
        text_primary="#ffb4a8",
        text_secondary="#cc8880",
        text_muted="#886060",
        accent="#ff5722",
        action="#ff5722",
        action_text="#ffffff",
        success="#66bb6a",
        danger="#ff8a80",
        warning="#ffcc80",
        border_color="#3a1010",
        border_input="#ff5722",
        border_row="#1f0505",
    ),
    "reef_green": Theme(
        id="reef_green",
        name="Reef Green",
        bg_primary="#001a0a",
        bg_secondary="#002a14",
        text_primary="#a8ffd0",
        text_secondary="#70c098",
        text_muted="#4a8068",
        accent="#00e676",
        action="#00e676",
        action_text="#001a0a",
        success="#69f0ae",
        danger="#ff8a80",
        warning="#ffd54f",
        border_color="#003a1a",
        border_input="#00e676",
        border_row="#001505",
    ),
    "daylight_amber": Theme(
        id="daylight_amber",
        name="Daylight Amber",
        bg_primary="#fffde7",
        bg_secondary="#fff9c4",
        text_primary="#3e2723",
        text_secondary="#6d4c41",
        text_muted="#9e9e9e",
        accent="#ff8f00",
        action="#ff8f00",
        action_text="#ffffff",
        success="#2e7d32",
        danger="#c62828",
        warning="#e65100",
        border_color="#ffecb3",
        border_input="#ff8f00",
        border_row="#fff3e0",
    ),
}

SYSTEM_DEFAULT_ID = "ocean_dark"


def theme_css(theme: Theme) -> str:
    """Return a minified :root { } CSS block for the given theme."""
    return (
        ":root{"
        f"--bg-primary:{theme.bg_primary};"
        f"--bg-secondary:{theme.bg_secondary};"
        f"--text-primary:{theme.text_primary};"
        f"--text-secondary:{theme.text_secondary};"
        f"--text-muted:{theme.text_muted};"
        f"--accent:{theme.accent};"
        f"--action:{theme.action};"
        f"--action-text:{theme.action_text};"
        f"--success:{theme.success};"
        f"--danger:{theme.danger};"
        f"--warning:{theme.warning};"
        f"--border-color:{theme.border_color};"
        f"--border-input:{theme.border_input};"
        f"--border-row:{theme.border_row};"
        "}"
    )


def resolve_theme(
    user_scheme: str | None,
    boat_default: str | None,
    custom_schemes: list[dict[str, str]],
) -> Theme:
    """Resolve the effective theme per the decision table in the spec.

    Priority: user override → boat default → system default (ocean_dark).
    A reference to a deleted custom scheme falls through to the next level.
    """
    custom_by_id = {str(s["id"]): s for s in custom_schemes}

    def _lookup(scheme_id: str | None) -> Theme | None:
        if not scheme_id:
            return None
        if scheme_id in BUILTIN_PRESETS:
            return BUILTIN_PRESETS[scheme_id]
        if scheme_id.startswith("custom:"):
            cid = scheme_id[7:]
            if cid in custom_by_id:
                c = custom_by_id[cid]
                return Theme(
                    id=scheme_id,
                    name=c["name"],
                    bg_primary=c["bg"],
                    bg_secondary=c["bg"],
                    text_primary=c["text_color"],
                    text_secondary=c["text_color"],
                    text_muted=c["text_color"],
                    accent=c["accent"],
                    action=c["accent"],
                    action_text=c["bg"],
                    success="#4ade80",
                    danger="#f87171",
                    warning="#fbbf24",
                    border_color=c["accent"],
                    border_input=c["accent"],
                    border_row=c["bg"],
                )
        return None

    for scheme_id in (user_scheme, boat_default):
        t = _lookup(scheme_id)
        if t is not None:
            return t
    return BUILTIN_PRESETS[SYSTEM_DEFAULT_ID]


def wcag_contrast(hex1: str, hex2: str) -> float:
    """Compute the WCAG 2.1 relative-luminance contrast ratio between two hex colors."""

    def _luminance(h: str) -> float:
        h = h.lstrip("#")
        r = int(h[0:2], 16) / 255
        g = int(h[2:4], 16) / 255
        b = int(h[4:6], 16) / 255

        def _lin(c: float) -> float:
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

        return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)

    l1 = _luminance(hex1)
    l2 = _luminance(hex2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)
