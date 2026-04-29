"""Storage tests for LLM transcript Q&A and callback tables (#697)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

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


async def _user(storage: Storage, email: str = "ada@example.com", role: str = "crew") -> int:
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO users (email, role, created_at) VALUES (?, ?, ?)",
        (email, role, "2026-01-01T12:00:00+00:00"),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


class TestLLMConsent:
    @pytest.mark.asyncio
    async def test_unacknowledged_by_default(self, storage: Storage) -> None:
        consent = await storage.get_llm_consent()
        assert consent is None

    @pytest.mark.asyncio
    async def test_acknowledge_records_user_and_time(self, storage: Storage) -> None:
        uid = await _user(storage, role="admin")
        await storage.acknowledge_llm_consent(user_id=uid)
        consent = await storage.get_llm_consent()
        assert consent is not None
        assert consent["by_user"] == uid
        assert consent["at"] is not None

    @pytest.mark.asyncio
    async def test_acknowledge_is_idempotent(self, storage: Storage) -> None:
        uid = await _user(storage, role="admin")
        await storage.acknowledge_llm_consent(user_id=uid)
        first = await storage.get_llm_consent()
        await storage.acknowledge_llm_consent(user_id=uid)
        second = await storage.get_llm_consent()
        # Re-ack remains acknowledged and stays attributed to the same user.
        # `at` may refresh on each call — that's fine.
        assert first is not None and second is not None
        assert first["by_user"] == second["by_user"] == uid


class TestLLMQAInsertList:
    @pytest.mark.asyncio
    async def test_insert_and_list_chronological(self, storage: Storage) -> None:
        rid = await _race(storage)
        uid = await _user(storage)
        q1 = await storage.insert_llm_qa(
            race_id=rid,
            user_id=uid,
            question="Summarize tactics",
            answer="We tacked twice.",
            citations=[{"ts": "2026-01-01T12:05:00+00:00", "label": "tack 1"}],
            model="claude-sonnet-4-6",
            input_tokens=1200,
            output_tokens=200,
            cache_read_tokens=1000,
            cache_create_tokens=200,
            cost_usd=0.012,
        )
        q2 = await storage.insert_llm_qa(
            race_id=rid,
            user_id=uid,
            question="Wind shifts?",
            answer="Two shifts.",
            citations=[],
            model="claude-sonnet-4-6",
            input_tokens=300,
            output_tokens=50,
            cache_read_tokens=300,
            cache_create_tokens=0,
            cost_usd=0.001,
        )
        rows = await storage.list_llm_qa(rid)
        assert [r["id"] for r in rows] == [q1, q2]
        assert rows[0]["question"] == "Summarize tactics"
        assert rows[0]["citations"] == [{"ts": "2026-01-01T12:05:00+00:00", "label": "tack 1"}]
        assert rows[0]["cost_usd"] == pytest.approx(0.012)

    @pytest.mark.asyncio
    async def test_list_scoped_to_race(self, storage: Storage) -> None:
        r1 = await _race(storage, n=1)
        r2 = await _race(storage, n=2)
        uid = await _user(storage)
        await storage.insert_llm_qa(
            race_id=r1,
            user_id=uid,
            question="q1",
            answer="a1",
            citations=[],
            model="m",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
            cost_usd=0.0,
        )
        rows = await storage.list_llm_qa(r2)
        assert rows == []

    @pytest.mark.asyncio
    async def test_failed_query_persisted_with_status(self, storage: Storage) -> None:
        rid = await _race(storage)
        uid = await _user(storage)
        await storage.insert_llm_qa(
            race_id=rid,
            user_id=uid,
            question="q",
            answer=None,
            citations=[],
            model="m",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
            cost_usd=0.0,
            status="failed",
            error_msg="rate_limit",
        )
        rows = await storage.list_llm_qa(rid)
        assert rows[0]["status"] == "failed"
        assert rows[0]["error_msg"] == "rate_limit"

    @pytest.mark.asyncio
    async def test_race_delete_cascades_qa(self, storage: Storage) -> None:
        rid = await _race(storage)
        uid = await _user(storage)
        await storage.insert_llm_qa(
            race_id=rid,
            user_id=uid,
            question="q",
            answer="a",
            citations=[],
            model="m",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
            cost_usd=0.0,
        )
        db = storage._conn()
        await db.execute("DELETE FROM races WHERE id = ?", (rid,))
        await db.commit()
        cur = await db.execute("SELECT COUNT(*) FROM llm_qa WHERE race_id = ?", (rid,))
        row = await cur.fetchone()
        assert row[0] == 0


class TestLLMCallbacks:
    @pytest.mark.asyncio
    async def test_replace_all_for_race(self, storage: Storage) -> None:
        rid = await _race(storage)
        await storage.replace_llm_callbacks(
            race_id=rid,
            callbacks=[
                {
                    "speaker_label": "speaker_0",
                    "anchor_ts": "2026-01-01T12:05:00+00:00",
                    "source_excerpt": "let's revisit that",
                    "rationale": "explicit revisit phrase",
                },
                {
                    "speaker_label": "speaker_1",
                    "anchor_ts": "2026-01-01T12:09:00+00:00",
                    "source_excerpt": "flag that one",
                    "rationale": "explicit flag phrase",
                },
            ],
            job_cost_usd=0.005,
        )
        first = await storage.list_llm_callbacks(rid)
        assert len(first) == 2

        await storage.replace_llm_callbacks(
            race_id=rid,
            callbacks=[
                {
                    "speaker_label": "speaker_0",
                    "anchor_ts": "2026-01-01T12:06:00+00:00",
                    "source_excerpt": "come back to this",
                    "rationale": "rerun",
                },
            ],
            job_cost_usd=0.004,
        )
        second = await storage.list_llm_callbacks(rid)
        assert len(second) == 1
        assert second[0]["source_excerpt"] == "come back to this"

    @pytest.mark.asyncio
    async def test_filter_by_speaker(self, storage: Storage) -> None:
        rid = await _race(storage)
        await storage.replace_llm_callbacks(
            race_id=rid,
            callbacks=[
                {
                    "speaker_label": "speaker_0",
                    "anchor_ts": "2026-01-01T12:05:00+00:00",
                    "source_excerpt": "x",
                    "rationale": "r",
                },
                {
                    "speaker_label": "speaker_1",
                    "anchor_ts": "2026-01-01T12:06:00+00:00",
                    "source_excerpt": "y",
                    "rationale": "r",
                },
            ],
            job_cost_usd=0.0,
        )
        only_zero = await storage.list_llm_callbacks(rid, speaker="speaker_0")
        assert len(only_zero) == 1
        assert only_zero[0]["speaker_label"] == "speaker_0"

    @pytest.mark.asyncio
    async def test_rerun_preserves_saved_moments(self, storage: Storage) -> None:
        """Re-running detection must not delete moment rows already created
        from a prior callback (spec §2 guard)."""
        rid = await _race(storage)
        await storage.replace_llm_callbacks(
            race_id=rid,
            callbacks=[
                {
                    "speaker_label": "speaker_0",
                    "anchor_ts": "2026-01-01T12:05:00+00:00",
                    "source_excerpt": "x",
                    "rationale": "r",
                }
            ],
            job_cost_usd=0.0,
        )
        cb = (await storage.list_llm_callbacks(rid))[0]
        mid = await storage.create_moment(
            session_id=rid,
            anchor_kind="timestamp",
            anchor_t_start="2026-01-01T12:05:00+00:00",
            subject="from callback",
        )
        await storage.link_llm_callback_moment(callback_id=cb["id"], moment_id=mid)

        await storage.replace_llm_callbacks(
            race_id=rid,
            callbacks=[],
            job_cost_usd=0.001,
        )
        m = await storage.get_moment(mid)
        assert m is not None
        assert m["subject"] == "from callback"


class TestCallbackJob:
    @pytest.mark.asyncio
    async def test_initial_state_is_not_run(self, storage: Storage) -> None:
        rid = await _race(storage)
        job = await storage.get_callback_job(rid)
        assert job is None or job["status"] == "NotRun"

    @pytest.mark.asyncio
    async def test_state_transitions(self, storage: Storage) -> None:
        rid = await _race(storage)
        await storage.set_callback_job(rid, status="Running")
        assert (await storage.get_callback_job(rid))["status"] == "Running"
        await storage.set_callback_job(rid, status="Complete", cost_usd=0.003)
        job = await storage.get_callback_job(rid)
        assert job["status"] == "Complete"
        assert job["cost_usd"] == pytest.approx(0.003)

    @pytest.mark.asyncio
    async def test_failed_records_error(self, storage: Storage) -> None:
        rid = await _race(storage)
        await storage.set_callback_job(rid, status="Failed", error_msg="cap_hit")
        job = await storage.get_callback_job(rid)
        assert job["status"] == "Failed"
        assert job["error_msg"] == "cap_hit"


class TestRaceCaps:
    @pytest.mark.asyncio
    async def test_no_override_returns_none(self, storage: Storage) -> None:
        rid = await _race(storage)
        assert await storage.get_race_caps(rid) is None

    @pytest.mark.asyncio
    async def test_set_then_get(self, storage: Storage) -> None:
        rid = await _race(storage)
        uid = await _user(storage, role="admin")
        await storage.set_race_caps(
            race_id=rid,
            soft_warn_usd=1.50,
            hard_cap_usd=5.00,
            by_user=uid,
        )
        caps = await storage.get_race_caps(rid)
        assert caps["soft_warn_usd"] == pytest.approx(1.50)
        assert caps["hard_cap_usd"] == pytest.approx(5.00)
        assert caps["updated_by"] == uid


class TestRaceCostAggregate:
    @pytest.mark.asyncio
    async def test_sum_qa_plus_job(self, storage: Storage) -> None:
        rid = await _race(storage)
        uid = await _user(storage)
        await storage.insert_llm_qa(
            race_id=rid,
            user_id=uid,
            question="q",
            answer="a",
            citations=[],
            model="m",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
            cost_usd=0.020,
        )
        await storage.insert_llm_qa(
            race_id=rid,
            user_id=uid,
            question="q",
            answer="a",
            citations=[],
            model="m",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
            cost_usd=0.005,
        )
        await storage.replace_llm_callbacks(
            race_id=rid,
            callbacks=[],
            job_cost_usd=0.010,
        )
        total = await storage.race_llm_cost(rid)
        assert total == pytest.approx(0.035)

    @pytest.mark.asyncio
    async def test_failed_qa_cost_still_counts(self, storage: Storage) -> None:
        """A failed query may have charged tokens before erroring — still count it
        so the per-race cap can't be bypassed by repeated failures."""
        rid = await _race(storage)
        uid = await _user(storage)
        await storage.insert_llm_qa(
            race_id=rid,
            user_id=uid,
            question="q",
            answer=None,
            citations=[],
            model="m",
            input_tokens=100,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
            cost_usd=0.001,
            status="failed",
            error_msg="timeout",
        )
        assert await storage.race_llm_cost(rid) == pytest.approx(0.001)

    @pytest.mark.asyncio
    async def test_zero_when_no_activity(self, storage: Storage) -> None:
        rid = await _race(storage)
        assert await storage.race_llm_cost(rid) == pytest.approx(0.0)
