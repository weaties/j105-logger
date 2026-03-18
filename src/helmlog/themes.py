"""Color scheme / theming system (#347).

Server-side theme resolution and CSS variable generation.  CSS variables are
injected via a ``<style>`` block in ``base.html`` on every page load — no
JavaScript theme-switching required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Preset registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThemeColors:
    """All CSS custom-property values for one color scheme."""

    id: str
    name: str
    bg_primary: str  # --bg-primary  (page background)
    text_primary: str  # --text-primary (body text)
    accent: str  # --accent      (links, highlights)
    bg_secondary: str  # --bg-secondary (cards/panels)
    text_secondary: str  # --text-secondary (muted text)
    border: str  # --border      (dividers)
    bg_input: str  # --bg-input    (form fields)
    accent_strong: str  # --accent-strong (buttons, focus rings)


PRESETS: dict[str, ThemeColors] = {
    tc.id: tc
    for tc in [
        ThemeColors(
            id="ocean_dark",
            name="Ocean Dark",
            bg_primary="#0a1628",
            text_primary="#e8eaf0",
            accent="#7eb8f7",
            bg_secondary="#131f35",
            text_secondary="#8892a4",
            border="#1e3a5f",
            bg_input="#0a1628",
            accent_strong="#2563eb",
        ),
        ThemeColors(
            id="sunlight",
            name="Sunlight High-Contrast",
            bg_primary="#FFFFFF",
            text_primary="#000000",
            accent="#0055AA",
            bg_secondary="#F5F5F5",
            text_secondary="#555555",
            border="#CCCCCC",
            bg_input="#FFFFFF",
            accent_strong="#0055AA",
        ),
        ThemeColors(
            id="racing_yellow",
            name="Racing Yellow",
            bg_primary="#000000",
            text_primary="#FFD600",
            accent="#FFD600",
            bg_secondary="#111111",
            text_secondary="#BBAA00",
            border="#333300",
            bg_input="#0a0a00",
            accent_strong="#FFD600",
        ),
        ThemeColors(
            id="sunset_red",
            name="Sunset Red",
            bg_primary="#1a0000",
            text_primary="#FFB4A8",
            accent="#FF5722",
            bg_secondary="#2a0808",
            text_secondary="#CC8880",
            border="#440000",
            bg_input="#1a0000",
            accent_strong="#FF5722",
        ),
        ThemeColors(
            id="reef_green",
            name="Reef Green",
            bg_primary="#001A0A",
            text_primary="#A8FFD0",
            accent="#00E676",
            bg_secondary="#0a2a10",
            text_secondary="#88CCAA",
            border="#003310",
            bg_input="#001A0A",
            accent_strong="#00E676",
        ),
        ThemeColors(
            id="daylight_amber",
            name="Daylight Amber",
            bg_primary="#FFFDE7",
            text_primary="#3E2723",
            accent="#FF8F00",
            bg_secondary="#FFF8C8",
            text_secondary="#6E4C3E",
            border="#D9C8A0",
            bg_input="#FFFDE7",
            accent_strong="#FF8F00",
        ),
    ]
}

SYSTEM_DEFAULT_ID = "ocean_dark"
PRESET_ORDER = ["sunlight", "racing_yellow", "ocean_dark", "sunset_red", "reef_green", "daylight_amber"]


# ---------------------------------------------------------------------------
# WCAG 2.1 contrast ratio
# ---------------------------------------------------------------------------


def _relative_luminance(hex_color: str) -> float:
    """Compute WCAG 2.1 relative luminance for a hex color."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r_raw, g_raw, b_raw = (int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))

    def _lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * _lin(r_raw) + 0.7152 * _lin(g_raw) + 0.0722 * _lin(b_raw)


def wcag_contrast(fg: str, bg: str) -> float:
    """Return the WCAG 2.1 contrast ratio between two hex colors (1.0–21.0)."""
    l1 = _relative_luminance(fg)
    l2 = _relative_luminance(bg)
    lighter, darker = (l1, l2) if l1 >= l2 else (l2, l1)
    return (lighter + 0.05) / (darker + 0.05)


# ---------------------------------------------------------------------------
# Custom scheme helpers
# ---------------------------------------------------------------------------


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02X}{g:02X}{b:02X}"


def _adjust_brightness(hex_color: str, delta: float) -> str:
    """Nudge all RGB channels by delta×255 (positive = lighter)."""
    r, g, b = _hex_to_rgb(hex_color)
    bump = int(delta * 255)
    return _rgb_to_hex(
        max(0, min(255, r + bump)),
        max(0, min(255, g + bump)),
        max(0, min(255, b + bump)),
    )


def _dim_color(hex_color: str, factor: float) -> str:
    """Blend hex_color toward its gray equivalent (factor=1 → original, 0 → gray)."""
    r, g, b = _hex_to_rgb(hex_color)
    avg = (r + g + b) // 3
    return _rgb_to_hex(
        int(r * factor + avg * (1 - factor)),
        int(g * factor + avg * (1 - factor)),
        int(b * factor + avg * (1 - factor)),
    )


def _custom_to_theme(cs: dict[str, Any]) -> ThemeColors:
    """Convert a custom scheme DB row into a ThemeColors instance."""
    bg = cs["bg"]
    text = cs["text_color"]
    accent = cs["accent"]
    return ThemeColors(
        id=f"custom:{cs['id']}",
        name=cs["name"],
        bg_primary=bg,
        text_primary=text,
        accent=accent,
        bg_secondary=_adjust_brightness(bg, 0.05),
        text_secondary=_dim_color(text, 0.6),
        border=_dim_color(text, 0.25),
        bg_input=bg,
        accent_strong=accent,
    )


# ---------------------------------------------------------------------------
# Theme resolution (decision table from spec)
# ---------------------------------------------------------------------------


def resolve_theme(
    user_scheme: str | None,
    boat_default: str | None,
    custom_schemes: list[dict[str, Any]],
) -> ThemeColors:
    """Resolve the active theme per the spec decision table.

    Resolution order:
    1. User's personal override (if the referenced scheme still exists)
    2. Boat default (if set and the referenced scheme still exists)
    3. System default (ocean_dark)

    Custom scheme IDs are stored as ``"custom:<integer id>"``.
    """
    custom_map: dict[str, dict[str, Any]] = {
        f"custom:{cs['id']}": cs for cs in custom_schemes
    }

    def _lookup(scheme: str | None) -> ThemeColors | None:
        if not scheme:
            return None
        if scheme in PRESETS:
            return PRESETS[scheme]
        cs = custom_map.get(scheme)
        if cs is not None:
            return _custom_to_theme(cs)
        return None  # scheme references a deleted / unknown entry

    # 1. User override
    if user_scheme:
        t = _lookup(user_scheme)
        if t is not None:
            return t

    # 2. Boat default
    if boat_default:
        t = _lookup(boat_default)
        if t is not None:
            return t

    # 3. System default
    return PRESETS[SYSTEM_DEFAULT_ID]


# ---------------------------------------------------------------------------
# CSS generation
# ---------------------------------------------------------------------------


def theme_to_css(theme: ThemeColors) -> str:
    """Return a minified CSS ``:root`` block that sets all theme variables."""
    return (
        ":root{"
        f"--bg-primary:{theme.bg_primary};"
        f"--text-primary:{theme.text_primary};"
        f"--accent:{theme.accent};"
        f"--bg-secondary:{theme.bg_secondary};"
        f"--text-secondary:{theme.text_secondary};"
        f"--border:{theme.border};"
        f"--bg-input:{theme.bg_input};"
        f"--accent-strong:{theme.accent_strong};"
        "}"
    )
