"""Storage tests for the unified moments primitive (#662)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from helmlog.anchors import AnchorError
from helmlog.storage import Storage, StorageConfig


@pytest_asyncio.fixture
async def storage() -> Storage:  # type: ignore[misc]
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


async def _race(storage: Storage, *, n: int = 1) -> int:
    race = await storage.start_race(
        f"T{n}",
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        "2026-01-01",
        n,
        f"race-{n}",
        "race",
    )
    assert race.id is not None
    return race.id


class TestCreateMoment:
    @pytest.mark.asyncio
    async def test_session_anchor(self, storage: Storage) -> None:
        sid = await _race(storage)
        mid = await storage.create_moment(
            session_id=sid,
            anchor_kind="session",
            subject="Race overview",
        )
        m = await storage.get_moment(mid)
        assert m is not None
        assert m["session_id"] == sid
        assert m["subject"] == "Race overview"
        assert m["anchor_kind"] == "session"
        assert m["anchor_t_start"] is None

    @pytest.mark.asyncio
    async def test_timestamp_anchor(self, storage: Storage) -> None:
        sid = await _race(storage)
        mid = await storage.create_moment(
            session_id=sid,
            anchor_kind="timestamp",
            anchor_t_start="2026-01-01T12:05:00+00:00",
        )
        m = await storage.get_moment(mid)
        assert m is not None
        assert m["anchor_t_start"] == "2026-01-01T12:05:00+00:00"

    @pytest.mark.asyncio
    async def test_timestamp_requires_t_start(self, storage: Storage) -> None:
        sid = await _race(storage)
        with pytest.raises(AnchorError):
            await storage.create_moment(session_id=sid, anchor_kind="timestamp")

    @pytest.mark.asyncio
    async def test_session_rejects_t_start(self, storage: Storage) -> None:
        sid = await _race(storage)
        with pytest.raises(AnchorError):
            await storage.create_moment(
                session_id=sid,
                anchor_kind="session",
                anchor_t_start="2026-01-01T12:05:00+00:00",
            )

    @pytest.mark.asyncio
    async def test_unknown_kind_rejected(self, storage: Storage) -> None:
        sid = await _race(storage)
        with pytest.raises(AnchorError):
            await storage.create_moment(session_id=sid, anchor_kind="nope")


class TestReadMoment:
    @pytest.mark.asyncio
    async def test_list_for_session(self, storage: Storage) -> None:
        sid = await _race(storage)
        m1 = await storage.create_moment(
            session_id=sid,
            anchor_kind="timestamp",
            anchor_t_start="2026-01-01T12:05:00+00:00",
            subject="a",
        )
        m2 = await storage.create_moment(
            session_id=sid,
            anchor_kind="timestamp",
            anchor_t_start="2026-01-01T12:06:00+00:00",
            subject="b",
        )
        moments = await storage.list_moments_for_session(sid)
        ids = [m["id"] for m in moments]
        assert ids == [m1, m2]  # t-ordered

    @pytest.mark.asyncio
    async def test_hides_unconfirmed_auto_moments_by_default(self, storage: Storage) -> None:
        sid = await _race(storage)
        await storage.create_moment(
            session_id=sid,
            anchor_kind="session",
            source="auto:transcript",
        )
        assert await storage.list_moments_for_session(sid) == []
        confirmed = await storage.list_moments_for_session(sid, include_unconfirmed=True)
        assert len(confirmed) == 1

    @pytest.mark.asyncio
    async def test_anchor_derive_for_maneuver(self, storage: Storage) -> None:
        sid = await _race(storage)
        # Seed a maneuver directly so we can attach a moment
        db = storage._conn()
        cur = await db.execute(
            "INSERT INTO maneuvers (session_id, type, ts, end_ts)"
            " VALUES (?, 'tack', '2026-01-01T12:08:00+00:00',"
            "         '2026-01-01T12:08:30+00:00')",
            (sid,),
        )
        await db.commit()
        maneuver_id = cur.lastrowid
        mid = await storage.create_moment(
            session_id=sid,
            anchor_kind="maneuver",
            anchor_entity_id=maneuver_id,
        )
        m = await storage.get_moment(mid)
        assert m is not None
        assert m["anchor_t_start"] == "2026-01-01T12:08:00+00:00"
        assert m["anchor_t_end"] == "2026-01-01T12:08:30+00:00"

    @pytest.mark.asyncio
    async def test_anchor_downgrade_when_maneuver_gone(self, storage: Storage) -> None:
        sid = await _race(storage)
        db = storage._conn()
        cur = await db.execute(
            "INSERT INTO maneuvers (session_id, type, ts)"
            " VALUES (?, 'tack', '2026-01-01T12:08:00+00:00')",
            (sid,),
        )
        await db.commit()
        mv_id = cur.lastrowid
        mid = await storage.create_moment(
            session_id=sid,
            anchor_kind="maneuver",
            anchor_entity_id=mv_id,
        )
        # write_maneuvers replaces the session's maneuvers, downgrading moments.
        await storage.write_maneuvers(sid, [])
        m = await storage.get_moment(mid)
        assert m is not None
        assert m["anchor_kind"] == "timestamp"
        assert m["anchor_entity_id"] is None


class TestMutateMoment:
    @pytest.mark.asyncio
    async def test_update_subject_and_counterparty(self, storage: Storage) -> None:
        sid = await _race(storage)
        mid = await storage.create_moment(session_id=sid, anchor_kind="session")
        await storage.update_moment(mid, subject="x", counterparty="Team Alpha")
        m = await storage.get_moment(mid)
        assert m["subject"] == "x"
        assert m["counterparty"] == "Team Alpha"

    @pytest.mark.asyncio
    async def test_clear_subject(self, storage: Storage) -> None:
        sid = await _race(storage)
        mid = await storage.create_moment(
            session_id=sid,
            anchor_kind="session",
            subject="keep?",
        )
        await storage.update_moment(mid, clear_subject=True)
        m = await storage.get_moment(mid)
        assert m["subject"] is None

    @pytest.mark.asyncio
    async def test_resolve_and_unresolve(self, storage: Storage) -> None:
        sid = await _race(storage)
        mid = await storage.create_moment(session_id=sid, anchor_kind="session")
        uid = await storage.create_user("a@b.co", "A", "admin")
        await storage.resolve_moment(mid, uid, "done")
        m = await storage.get_moment(mid)
        assert m["resolved"] == 1
        assert m["resolved_by"] == uid
        assert m["resolution_summary"] == "done"
        await storage.unresolve_moment(mid)
        m = await storage.get_moment(mid)
        assert m["resolved"] == 0

    @pytest.mark.asyncio
    async def test_confirm_moment(self, storage: Storage) -> None:
        sid = await _race(storage)
        mid = await storage.create_moment(
            session_id=sid,
            anchor_kind="session",
            source="auto:transcript",
        )
        uid = await storage.create_user("a@b.co", "A", "admin")
        await storage.confirm_moment(mid, uid)
        m = await storage.get_moment(mid)
        assert m["confirmed_by"] == uid
        assert m["confirmed_at"] is not None

    @pytest.mark.asyncio
    async def test_delete_moment(self, storage: Storage) -> None:
        sid = await _race(storage)
        mid = await storage.create_moment(session_id=sid, anchor_kind="session")
        assert await storage.delete_moment(mid) is True
        assert await storage.get_moment(mid) is None


class TestCounterparties:
    @pytest.mark.asyncio
    async def test_distinct_typeahead(self, storage: Storage) -> None:
        sid = await _race(storage)
        await storage.create_moment(
            session_id=sid,
            anchor_kind="session",
            counterparty="Cyclops",
        )
        await storage.create_moment(
            session_id=sid,
            anchor_kind="session",
            counterparty="Cyclops",
        )
        await storage.create_moment(
            session_id=sid,
            anchor_kind="session",
            counterparty="Asterisk",
        )
        assert await storage.list_moment_counterparties() == ["Asterisk", "Cyclops"]
