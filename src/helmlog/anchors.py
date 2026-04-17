"""Anchor schema — points into a session's timeline.

An anchor says *where on the timeline* a bookmark, thread, or tag-applied
entity lives. It is intentionally pure data: no storage lookups, no FK
checks, no coupling to other modules. Existence of referenced entities
(e.g. a `maneuver.id`) is verified by the caller when the anchor is
persisted — here we only enforce structural validity.

See the spec on issue #477 for the full decision table. Each valid/invalid
row in the table corresponds to a unit test in `tests/test_anchors.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

KNOWN_KINDS: frozenset[str] = frozenset(
    {"timestamp", "segment", "maneuver", "rounding", "start", "race", "bookmark"}
)

_ENTITY_REF_KINDS: frozenset[str] = frozenset({"maneuver", "rounding", "start", "race", "bookmark"})

_FIELDS: frozenset[str] = frozenset({"kind", "entity_id", "t_start", "t_end"})


class AnchorError(ValueError):
    """Raised when an Anchor fails structural validation."""


@dataclass(frozen=True, slots=True)
class Anchor:
    kind: str
    entity_id: int | None = None
    t_start: str | None = None
    t_end: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Anchor:
        extra = set(data.keys()) - _FIELDS
        if extra:
            raise AnchorError(f"unexpected anchor fields: {sorted(extra)}")
        return cls(
            kind=data.get("kind", ""),
            entity_id=data.get("entity_id"),
            t_start=data.get("t_start"),
            t_end=data.get("t_end"),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.entity_id is not None:
            out["entity_id"] = self.entity_id
        if self.t_start is not None:
            out["t_start"] = self.t_start
        if self.t_end is not None:
            out["t_end"] = self.t_end
        return out


def validate_anchor(anchor: Anchor) -> None:
    """Validate an anchor against the decision table.

    Raises AnchorError with a human-readable message naming the offending
    field. Returns None on success.
    """
    kind = anchor.kind
    if kind not in KNOWN_KINDS:
        raise AnchorError(f"unknown anchor kind: {kind!r}")

    if kind == "timestamp":
        if anchor.t_start is None:
            raise AnchorError("timestamp anchor requires t_start")
        if anchor.entity_id is not None:
            raise AnchorError("timestamp anchor must not set entity_id")
        if anchor.t_end is not None:
            raise AnchorError("timestamp anchor must not set t_end")
        return

    if kind == "segment":
        if anchor.t_start is None:
            raise AnchorError("segment anchor requires t_start")
        if anchor.t_end is None:
            raise AnchorError("segment anchor requires t_end")
        if anchor.entity_id is not None:
            raise AnchorError("segment anchor must not set entity_id")
        if anchor.t_end <= anchor.t_start:
            raise AnchorError("segment anchor t_end must be after t_start")
        return

    if kind in _ENTITY_REF_KINDS:
        if anchor.entity_id is None:
            raise AnchorError(f"{kind} anchor requires entity_id")
        if anchor.t_start is not None:
            raise AnchorError(f"{kind} anchor must not set t_start")
        if anchor.t_end is not None:
            raise AnchorError(f"{kind} anchor must not set t_end")
        return

    # Should be unreachable given KNOWN_KINDS check above.
    raise AnchorError(f"unknown anchor kind: {kind!r}")
