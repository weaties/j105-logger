"""Claude-powered Q&A assistant for HelmLog admins (#429).

Wraps the Anthropic SDK to answer questions about how HelmLog works,
using dynamic codebase context. Read-only — never suggests code changes.
"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

# ---------------------------------------------------------------------------
# Source file directory
# ---------------------------------------------------------------------------

_SRC_DIR = Path(__file__).parent
_PROJECT_ROOT = _SRC_DIR.parent.parent
_CLAUDE_MD = _PROJECT_ROOT / "CLAUDE.md"

# ---------------------------------------------------------------------------
# Keyword → module mapping
# ---------------------------------------------------------------------------

KEYWORD_MAP: dict[str, list[str]] = {
    "polar": ["polar.py"],
    "performance": ["polar.py"],
    "target speed": ["polar.py"],
    "vmg": ["polar.py"],
    "signal k": ["sk_reader.py"],
    "signalk": ["sk_reader.py"],
    "sk reader": ["sk_reader.py"],
    "websocket": ["sk_reader.py"],
    "instrument data": ["sk_reader.py"],
    "can": ["can_reader.py", "nmea2000.py"],
    "nmea": ["can_reader.py", "nmea2000.py"],
    "pgn": ["can_reader.py", "nmea2000.py"],
    "canbus": ["can_reader.py", "nmea2000.py"],
    "storage": ["storage.py"],
    "database": ["storage.py"],
    "sqlite": ["storage.py"],
    "migration": ["storage.py"],
    "schema": ["storage.py"],
    "audio": ["audio.py"],
    "recording": ["audio.py"],
    "microphone": ["audio.py"],
    "transcri": ["transcribe.py"],
    "whisper": ["transcribe.py"],
    "diari": ["transcribe.py"],
    "export": ["export.py"],
    "csv": ["export.py"],
    "gpx": ["export.py"],
    "json export": ["export.py"],
    "weather": ["external.py"],
    "tide": ["external.py"],
    "wind forecast": ["external.py"],
    "federation": ["federation.py", "peer_api.py", "peer_client.py", "peer_auth.py"],
    "co-op": ["federation.py", "peer_api.py", "peer_client.py", "peer_auth.py"],
    "peer": ["federation.py", "peer_api.py", "peer_client.py", "peer_auth.py"],
    "boat identity": ["federation.py"],
    "auth": ["auth.py"],
    "login": ["auth.py"],
    "session cookie": ["auth.py"],
    "magic link": ["auth.py"],
    "camera": ["cameras.py", "insta360.py"],
    "insta360": ["cameras.py", "insta360.py"],
    "video": ["video.py", "insta360.py"],
    "aruco": ["aruco_detector.py"],
    "marker": ["aruco_detector.py"],
    "esp32": ["aruco_detector.py"],
    "race": ["races.py", "race_classifier.py"],
    "mark": ["races.py"],
    "start": ["races.py"],
    "finish": ["races.py"],
    "network": ["network.py"],
    "wlan": ["network.py"],
    "wifi": ["network.py"],
    "tailscale": ["network.py"],
    "deploy": ["deploy.py"],
    "setup": ["deploy.py"],
    "web": ["web.py"],
    "route": ["web.py"],
    "api": ["web.py"],
    "endpoint": ["web.py"],
    "template": ["web.py"],
    "html": ["web.py"],
    "rudder": ["nmea2000.py"],
    "steering": ["nmea2000.py"],
    "trigger": ["triggers.py"],
    "auto start": ["triggers.py"],
    "auto stop": ["triggers.py"],
    "monitor": ["monitor.py", "influx.py"],
    "influx": ["monitor.py", "influx.py"],
    "system health": ["monitor.py", "influx.py"],
    "maneuver": ["maneuver_detector.py"],
    "tack": ["maneuver_detector.py"],
    "gybe": ["maneuver_detector.py"],
    "course": ["courses.py"],
}

# Maximum total characters for included source context
MAX_CONTEXT_CHARS = 60_000

# Maximum number of lines to include per file (large files get truncated)
_MAX_LINES_PER_FILE = 150

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def is_configured() -> bool:
    """Return True if the Anthropic API key is set."""
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def select_source_files(question: str) -> list[str]:
    """Return a deduplicated list of module filenames relevant to the question.

    Matches are case-insensitive against KEYWORD_MAP.
    """
    q_lower = question.lower()
    seen: set[str] = set()
    result: list[str] = []
    for keyword, modules in KEYWORD_MAP.items():
        if keyword in q_lower:
            for m in modules:
                if m not in seen:
                    seen.add(m)
                    result.append(m)
    return result


def build_module_index() -> str:
    """Return a one-line-per-module index: filename + first docstring line."""
    lines: list[str] = []
    for py in sorted(_SRC_DIR.glob("*.py")):
        if py.name.startswith("_"):
            continue
        first_doc = ""
        try:
            text = py.read_text()
            if text.startswith('"""'):
                end = text.index('"""', 3)
                first_doc = text[3:end].split("\n")[0].strip()
        except Exception:  # noqa: BLE001
            pass
        lines.append(f"- {py.name}: {first_doc}")
    return "\n".join(lines)


def _read_file_truncated(path: Path) -> str:
    """Read a file, truncating to _MAX_LINES_PER_FILE lines."""
    try:
        text = path.read_text()
    except OSError:
        return ""
    lines = text.splitlines(keepends=True)
    if len(lines) > _MAX_LINES_PER_FILE:
        truncated = "".join(lines[:_MAX_LINES_PER_FILE])
        return f"{truncated}\n... (truncated at {_MAX_LINES_PER_FILE} lines)\n"
    return text


def build_system_prompt(question: str) -> str:
    """Assemble the system prompt with CLAUDE.md, module index, and relevant sources."""
    parts: list[str] = []

    parts.append(
        "You are a helpful assistant that explains how HelmLog works. "
        "You answer questions about the codebase, architecture, and sailing data. "
        "You do NOT suggest code changes or produce diffs. "
        "Keep answers concise and focused."
    )

    # CLAUDE.md
    try:
        claude_md = _CLAUDE_MD.read_text()
        parts.append(f"## Project Overview\n\n{claude_md}")
    except OSError:
        pass

    # Module index
    parts.append(f"## Module Index\n\n{build_module_index()}")

    # Relevant source files
    selected = select_source_files(question)
    if selected:
        parts.append("## Relevant Source Code\n")
        total_chars = sum(len(p) for p in parts)
        for filename in selected:
            path = _SRC_DIR / filename
            content = _read_file_truncated(path)
            if not content:
                continue
            if total_chars + len(content) + 100 > MAX_CONTEXT_CHARS:
                break
            section = f"### {filename}\n\n```python\n{content}```"
            parts.append(section)
            total_chars += len(section)

    return "\n\n".join(parts)


async def chat(
    messages: list[dict[str, str]],
) -> str:
    """Send a conversation to Claude Haiku and return the assistant response.

    *messages* is a list of ``{"role": "user"|"assistant", "content": "..."}`` dicts.
    The last message must be from the user.

    Raises ``RuntimeError`` if the API key is not configured.
    Raises ``anthropic.APIError`` on upstream failures.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")

    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key)

    # Build system prompt from the latest user message
    latest_question = messages[-1]["content"] if messages else ""
    system_prompt = build_system_prompt(latest_question)

    n_files = len(select_source_files(latest_question))
    logger.debug("Assistant: {} source files selected for context", n_files)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=system_prompt,
        messages=[
            {"role": m["role"], "content": m["content"]}  # type: ignore[typeddict-item]
            for m in messages
        ],
    )

    return response.content[0].text  # type: ignore[union-attr]
