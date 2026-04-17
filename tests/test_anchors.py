"""Unit tests for the Anchor validator.

Decision table (from /spec on #477):

| kind        | entity_id          | t_start          | t_end      | Valid? |
|-------------|--------------------|------------------|------------|--------|
| timestamp   | null               | required         | null       | yes    |
| timestamp   | any other shape    | ...              | ...        | no     |
| segment     | null               | required         | > t_start  | yes    |
| segment     | null               | required         | <= t_start | no     |
| maneuver    | required           | null             | null       | yes    |
| rounding    | required           | null             | null       | yes    |
| start       | required (race id) | null             | null       | yes    |
| race        | required (race id) | null             | null       | yes    |
| bookmark    | required           | null             | null       | yes    |
| known kind  | missing field      | ...              | ...        | no     |
| unknown     | any                | any              | any        | no     |
"""

from __future__ import annotations

import pytest

from helmlog.anchors import Anchor, AnchorError, validate_anchor

_T0 = "2024-06-15T12:00:00Z"
_T1 = "2024-06-15T12:00:05Z"


def test_timestamp_valid() -> None:
    a = Anchor(kind="timestamp", t_start=_T0)
    validate_anchor(a)


def test_timestamp_rejects_entity_id() -> None:
    with pytest.raises(AnchorError, match="timestamp.*entity_id"):
        validate_anchor(Anchor(kind="timestamp", entity_id=5, t_start=_T0))


def test_timestamp_rejects_t_end() -> None:
    with pytest.raises(AnchorError, match="timestamp.*t_end"):
        validate_anchor(Anchor(kind="timestamp", t_start=_T0, t_end=_T1))


def test_timestamp_requires_t_start() -> None:
    with pytest.raises(AnchorError, match="t_start"):
        validate_anchor(Anchor(kind="timestamp"))


def test_segment_valid() -> None:
    validate_anchor(Anchor(kind="segment", t_start=_T0, t_end=_T1))


def test_segment_rejects_equal_bounds() -> None:
    with pytest.raises(AnchorError, match="t_end.*after.*t_start"):
        validate_anchor(Anchor(kind="segment", t_start=_T0, t_end=_T0))


def test_segment_rejects_inverted_bounds() -> None:
    with pytest.raises(AnchorError, match="t_end.*after.*t_start"):
        validate_anchor(Anchor(kind="segment", t_start=_T1, t_end=_T0))


def test_segment_requires_t_end() -> None:
    with pytest.raises(AnchorError, match="t_end"):
        validate_anchor(Anchor(kind="segment", t_start=_T0))


def test_segment_requires_t_start() -> None:
    with pytest.raises(AnchorError, match="t_start"):
        validate_anchor(Anchor(kind="segment", t_end=_T1))


def test_segment_rejects_entity_id() -> None:
    with pytest.raises(AnchorError, match="segment.*entity_id"):
        validate_anchor(Anchor(kind="segment", entity_id=1, t_start=_T0, t_end=_T1))


@pytest.mark.parametrize("kind", ["maneuver", "rounding", "start", "race", "bookmark"])
def test_entity_ref_kinds_valid(kind: str) -> None:
    validate_anchor(Anchor(kind=kind, entity_id=42))


@pytest.mark.parametrize("kind", ["maneuver", "rounding", "start", "race", "bookmark"])
def test_entity_ref_kinds_require_entity_id(kind: str) -> None:
    with pytest.raises(AnchorError, match="entity_id"):
        validate_anchor(Anchor(kind=kind))


@pytest.mark.parametrize("kind", ["maneuver", "rounding", "start", "race", "bookmark"])
def test_entity_ref_kinds_reject_t_start(kind: str) -> None:
    with pytest.raises(AnchorError, match="t_start"):
        validate_anchor(Anchor(kind=kind, entity_id=1, t_start=_T0))


@pytest.mark.parametrize("kind", ["maneuver", "rounding", "start", "race", "bookmark"])
def test_entity_ref_kinds_reject_t_end(kind: str) -> None:
    with pytest.raises(AnchorError, match="t_end"):
        validate_anchor(Anchor(kind=kind, entity_id=1, t_end=_T1))


def test_unknown_kind_rejected() -> None:
    with pytest.raises(AnchorError, match="unknown anchor kind"):
        validate_anchor(Anchor(kind="nope", t_start=_T0))


def test_empty_kind_rejected() -> None:
    with pytest.raises(AnchorError, match="unknown anchor kind"):
        validate_anchor(Anchor(kind=""))


def test_anchor_from_dict_round_trip() -> None:
    a = Anchor.from_dict({"kind": "timestamp", "t_start": _T0})
    assert a.kind == "timestamp"
    assert a.t_start == _T0
    assert a.entity_id is None
    assert a.t_end is None


def test_anchor_from_dict_rejects_extra_keys() -> None:
    with pytest.raises(AnchorError, match="unexpected"):
        Anchor.from_dict({"kind": "timestamp", "t_start": _T0, "extra": "oops"})


def test_anchor_to_dict_strips_none() -> None:
    a = Anchor(kind="timestamp", t_start=_T0)
    d = a.to_dict()
    assert d == {"kind": "timestamp", "t_start": _T0}


def test_anchor_to_dict_full() -> None:
    a = Anchor(kind="segment", t_start=_T0, t_end=_T1)
    assert a.to_dict() == {"kind": "segment", "t_start": _T0, "t_end": _T1}


def test_anchor_known_kinds_constant() -> None:
    from helmlog.anchors import KNOWN_KINDS

    assert (
        frozenset({"timestamp", "segment", "maneuver", "rounding", "start", "race", "bookmark"})
        == KNOWN_KINDS
    )
