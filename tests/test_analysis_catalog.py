"""Tests for Phase 2 analysis catalog lifecycle (#285).

Decision table rows → test cases
State machine transitions → test cases
EARS requirements → test cases
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio

from helmlog.analysis.catalog import (
    ACTIVE_STATES,
    BOAT_LOCAL,
    CO_OP_ACTIVE,
    CO_OP_DEFAULT,
    DEPRECATED,
    PROPOSED,
    REJECTED,
    CatalogEntry,
    CatalogError,
    approve,
    check_data_license_gate,
    deprecate,
    propose_to_co_op,
    reject,
    restore,
    set_co_op_default,
    unset_co_op_default,
)
from helmlog.analysis.cache import AnalysisCache
from helmlog.analysis.protocol import PluginMeta
from helmlog.storage import Storage, StorageConfig
from helmlog.web import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def storage() -> Storage:  # type: ignore[misc]
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


async def _seed_session(storage: Storage) -> int:
    """Create a completed session. Returns race_id."""
    race = await storage.start_race(
        "Test",
        datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC),
        "2024-06-15",
        1,
        "Test Race",
        "race",
    )
    race_id = race.id
    db = storage._conn()
    for i in range(5):
        ts = f"2024-06-15T12:00:{i:02d}"
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts, race_id) VALUES (?, 5, ?, ?)",
            (ts, 5.0 + i * 0.1, race_id),
        )
        await db.execute(
            "INSERT INTO winds"
            " (ts, source_addr, wind_speed_kts, wind_angle_deg, reference, race_id)"
            " VALUES (?, 5, ?, ?, 0, ?)",
            (ts, 12.0, 45.0, race_id),
        )
    await db.commit()
    await storage.end_race(race_id, datetime(2024, 6, 15, 12, 5, 0, tzinfo=UTC))
    return race_id


async def _seed_co_op(storage: Storage, co_op_id: str = "coop1", role: str = "admin") -> None:
    """Seed a co-op membership row."""
    await storage.save_co_op_membership(
        co_op_id=co_op_id,
        co_op_name="Test Co-op",
        co_op_pub="pub_test",
        membership_json="{}",
        role=role,
    )


async def _seed_user(storage: Storage) -> int:
    return await storage.create_user("test@example.com", "Test User", "crew")


# Clean result sample that passes the data license gate
_CLEAN_RESULT: dict[str, Any] = {
    "plugin_name": "polar_baseline",
    "plugin_version": "1.0.0",
    "session_id": 1,
    "metrics": [{"name": "avg_bsp", "value": 5.2, "unit": "kts", "label": "Avg BSP"}],
    "insights": [],
    "viz": [],
    "raw": {"bins": {}},
}

# PII-tainted result sample (should fail data license gate)
_PII_RESULT: dict[str, Any] = {
    "plugin_name": "bad_plugin",
    "plugin_version": "1.0.0",
    "session_id": 1,
    "metrics": [{"name": "audio_score", "value": 0.8, "unit": "", "label": "Audio"}],
    "insights": [],
    "viz": [],
    "raw": {"transcript_words": []},
}


# ---------------------------------------------------------------------------
# PluginMeta — author/changelog fields (#285)
# ---------------------------------------------------------------------------


class TestPluginMetaExtensions:
    def test_author_defaults_to_empty(self) -> None:
        m = PluginMeta(name="test", display_name="Test", description="d", version="1.0")
        assert m.author == ""

    def test_changelog_defaults_to_empty(self) -> None:
        m = PluginMeta(name="test", display_name="Test", description="d", version="1.0")
        assert m.changelog == ""

    def test_author_and_changelog_can_be_set(self) -> None:
        m = PluginMeta(
            name="test",
            display_name="Test",
            description="d",
            version="2.0",
            author="weaties",
            changelog="Initial release",
        )
        assert m.author == "weaties"
        assert m.changelog == "Initial release"

    def test_still_frozen(self) -> None:
        m = PluginMeta(name="t", display_name="T", description="d", version="1.0")
        with pytest.raises(AttributeError):
            m.author = "hacker"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Data licensing gate
# ---------------------------------------------------------------------------


class TestDataLicenseGate:
    def test_clean_result_passes(self) -> None:
        failing = check_data_license_gate(_CLEAN_RESULT)
        assert failing == []

    def test_pii_metric_name_fails(self) -> None:
        result = {
            "metrics": [{"name": "audio_score", "value": 0.8, "unit": ""}],
            "raw": {},
        }
        failing = check_data_license_gate(result)
        assert any("metric:audio_score" in f for f in failing)

    def test_pii_raw_key_fails(self) -> None:
        result: dict[str, Any] = {
            "metrics": [],
            "raw": {"transcript_words": ["hello"]},
        }
        failing = check_data_license_gate(result)
        assert any("raw:transcript_words" in f for f in failing)

    def test_multiple_pii_fields_all_reported(self) -> None:
        result: dict[str, Any] = {
            "metrics": [
                {"name": "biometric_hr", "value": 75, "unit": "bpm"},
                {"name": "photo_count", "value": 10, "unit": ""},
            ],
            "raw": {"audio_features": {}},
        }
        failing = check_data_license_gate(result)
        assert len(failing) == 3

    def test_case_insensitive(self) -> None:
        result: dict[str, Any] = {
            "metrics": [{"name": "AUDIO_LEVEL", "value": 1.0, "unit": ""}],
            "raw": {},
        }
        failing = check_data_license_gate(result)
        assert len(failing) == 1

    def test_partial_match_in_name(self) -> None:
        # "notes" appears inside "boat_notes" — should flag it
        result: dict[str, Any] = {
            "metrics": [],
            "raw": {"boat_notes": "tack at mark"},
        }
        failing = check_data_license_gate(result)
        assert len(failing) == 1

    def test_non_pii_name_passes(self) -> None:
        result: dict[str, Any] = {
            "metrics": [{"name": "vmg_mean", "value": 3.2, "unit": "kts"}],
            "raw": {"bins": {}},
        }
        assert check_data_license_gate(result) == []


# ---------------------------------------------------------------------------
# State machine transitions
# ---------------------------------------------------------------------------


class TestCatalogStateMachine:
    @pytest.mark.asyncio
    async def test_propose_creates_entry_in_proposed_state(self, storage: Storage) -> None:
        """Decision table row: boat owner proposes plugin → proposed state."""
        entry = await propose_to_co_op(
            storage,
            "polar_baseline",
            "coop1",
            proposing_boat="fingerprint_a",
            version="1.0.0",
        )
        assert entry.state == PROPOSED
        assert entry.plugin_name == "polar_baseline"
        assert entry.co_op_id == "coop1"
        assert entry.proposing_boat == "fingerprint_a"

    @pytest.mark.asyncio
    async def test_propose_twice_raises_if_not_rejected(self, storage: Storage) -> None:
        """Cannot re-propose a plugin already in proposed/active/deprecated states."""
        await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        with pytest.raises(CatalogError, match="already in state"):
            await propose_to_co_op(
                storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
            )

    @pytest.mark.asyncio
    async def test_approve_proposed_transitions_to_co_op_active(self, storage: Storage) -> None:
        """Decision table row: moderator approves proposed plugin."""
        await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        entry = await approve(
            storage, "polar_baseline", "coop1", result_sample=_CLEAN_RESULT
        )
        assert entry.state == CO_OP_ACTIVE
        assert entry.data_license_gate_passed is True

    @pytest.mark.asyncio
    async def test_approve_blocked_by_pii_data(self, storage: Storage) -> None:
        """EARS: data license gate blocks Proposed → CoopActive when PII fields present."""
        await propose_to_co_op(
            storage, "bad_plugin", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        with pytest.raises(CatalogError, match="Data license gate failed"):
            await approve(storage, "bad_plugin", "coop1", result_sample=_PII_RESULT)

    @pytest.mark.asyncio
    async def test_approve_not_proposed_raises(self, storage: Storage) -> None:
        """Cannot approve a plugin that isn't in 'proposed' state."""
        with pytest.raises(CatalogError, match="must be in 'proposed' state"):
            await approve(storage, "polar_baseline", "coop1", result_sample=_CLEAN_RESULT)

    @pytest.mark.asyncio
    async def test_reject_proposed_transitions_to_rejected(self, storage: Storage) -> None:
        """Decision table row: moderator rejects proposed plugin with reason."""
        await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        entry = await reject(storage, "polar_baseline", "coop1", reason="Needs more testing")
        assert entry.state == REJECTED
        assert entry.reject_reason == "Needs more testing"

    @pytest.mark.asyncio
    async def test_reject_not_proposed_raises(self, storage: Storage) -> None:
        """Cannot reject a plugin that isn't proposed."""
        with pytest.raises(CatalogError, match="must be in 'proposed' state"):
            await reject(storage, "polar_baseline", "coop1", reason="bad")

    @pytest.mark.asyncio
    async def test_re_propose_after_rejection(self, storage: Storage) -> None:
        """Decision table: author fixes and re-proposes after rejection."""
        await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        await reject(storage, "polar_baseline", "coop1", reason="Bug in output")
        # Should be allowed to re-propose
        entry = await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.1.0"
        )
        assert entry.state == PROPOSED
        assert entry.version == "1.1.0"

    @pytest.mark.asyncio
    async def test_set_co_op_default(self, storage: Storage) -> None:
        """Decision table: moderator sets co_op_active as default."""
        await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        await approve(storage, "polar_baseline", "coop1", result_sample=_CLEAN_RESULT)
        entry = await set_co_op_default(storage, "polar_baseline", "coop1")
        assert entry.state == CO_OP_DEFAULT

    @pytest.mark.asyncio
    async def test_set_default_clears_previous_default(self, storage: Storage) -> None:
        """Setting a new default implicitly unsets the previous one."""
        for name in ("polar_baseline", "sail_vmg"):
            await propose_to_co_op(
                storage, name, "coop1", proposing_boat="fp_a", version="1.0.0"
            )
            await approve(storage, name, "coop1", result_sample=_CLEAN_RESULT)

        await set_co_op_default(storage, "polar_baseline", "coop1")
        # Now set sail_vmg as default — polar_baseline should revert to co_op_active
        await set_co_op_default(storage, "sail_vmg", "coop1")

        polar_row = await storage.get_catalog_entry("polar_baseline", "coop1")
        vmg_row = await storage.get_catalog_entry("sail_vmg", "coop1")
        assert polar_row is not None and polar_row["state"] == CO_OP_ACTIVE
        assert vmg_row is not None and vmg_row["state"] == CO_OP_DEFAULT

    @pytest.mark.asyncio
    async def test_set_default_requires_co_op_active_state(self, storage: Storage) -> None:
        """Cannot set default for a plugin that isn't co_op_active."""
        await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        with pytest.raises(CatalogError, match="must be co_op_active"):
            await set_co_op_default(storage, "polar_baseline", "coop1")

    @pytest.mark.asyncio
    async def test_unset_co_op_default(self, storage: Storage) -> None:
        """Decision table: moderator unsets co-op default (reverts to platform default)."""
        await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        await approve(storage, "polar_baseline", "coop1", result_sample=_CLEAN_RESULT)
        await set_co_op_default(storage, "polar_baseline", "coop1")
        entry = await unset_co_op_default(storage, "polar_baseline", "coop1")
        assert entry.state == CO_OP_ACTIVE

    @pytest.mark.asyncio
    async def test_deprecate_co_op_active(self, storage: Storage) -> None:
        """Decision table: moderator/author deprecates co_op_active plugin."""
        await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        await approve(storage, "polar_baseline", "coop1", result_sample=_CLEAN_RESULT)
        entry = await deprecate(storage, "polar_baseline", "coop1")
        assert entry.state == DEPRECATED

    @pytest.mark.asyncio
    async def test_deprecate_co_op_default(self, storage: Storage) -> None:
        """Decision table: deprecating a co_op_default plugin also removes default."""
        await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        await approve(storage, "polar_baseline", "coop1", result_sample=_CLEAN_RESULT)
        await set_co_op_default(storage, "polar_baseline", "coop1")
        entry = await deprecate(storage, "polar_baseline", "coop1")
        assert entry.state == DEPRECATED

    @pytest.mark.asyncio
    async def test_deprecate_boat_local_raises(self, storage: Storage) -> None:
        """Cannot deprecate a plugin that is not co_op_active or co_op_default."""
        await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        with pytest.raises(CatalogError, match="must be co_op_active or co_op_default"):
            await deprecate(storage, "polar_baseline", "coop1")

    @pytest.mark.asyncio
    async def test_restore_deprecated(self, storage: Storage) -> None:
        """Decision table: moderator restores a deprecated plugin."""
        await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        await approve(storage, "polar_baseline", "coop1", result_sample=_CLEAN_RESULT)
        await deprecate(storage, "polar_baseline", "coop1")
        entry = await restore(storage, "polar_baseline", "coop1")
        assert entry.state == CO_OP_ACTIVE

    @pytest.mark.asyncio
    async def test_restore_non_deprecated_raises(self, storage: Storage) -> None:
        await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        with pytest.raises(CatalogError, match="must be deprecated"):
            await restore(storage, "polar_baseline", "coop1")


# ---------------------------------------------------------------------------
# Active states constant
# ---------------------------------------------------------------------------


class TestActiveStates:
    def test_boat_local_is_active(self) -> None:
        assert BOAT_LOCAL in ACTIVE_STATES

    def test_co_op_active_is_active(self) -> None:
        assert CO_OP_ACTIVE in ACTIVE_STATES

    def test_co_op_default_is_active(self) -> None:
        assert CO_OP_DEFAULT in ACTIVE_STATES

    def test_proposed_not_active(self) -> None:
        assert PROPOSED not in ACTIVE_STATES

    def test_rejected_not_active(self) -> None:
        assert REJECTED not in ACTIVE_STATES

    def test_deprecated_not_active(self) -> None:
        assert DEPRECATED not in ACTIVE_STATES


# ---------------------------------------------------------------------------
# Storage catalog CRUD
# ---------------------------------------------------------------------------


class TestStorageCatalog:
    @pytest.mark.asyncio
    async def test_get_catalog_entry_not_found(self, storage: Storage) -> None:
        result = await storage.get_catalog_entry("nonexistent", "coop1")
        assert result is None

    @pytest.mark.asyncio
    async def test_upsert_and_get(self, storage: Storage) -> None:
        now = datetime.now(UTC).isoformat()
        await storage.upsert_catalog_entry(
            plugin_name="polar_baseline",
            co_op_id="coop1",
            state=PROPOSED,
            proposing_boat="fp_a",
            version="1.0.0",
            author="weaties",
            changelog="first",
            proposed_at=now,
            resolved_at=None,
            reject_reason=None,
            data_license_gate_passed=0,
        )
        row = await storage.get_catalog_entry("polar_baseline", "coop1")
        assert row is not None
        assert row["state"] == PROPOSED
        assert row["author"] == "weaties"

    @pytest.mark.asyncio
    async def test_list_catalog_entries(self, storage: Storage) -> None:
        now = datetime.now(UTC).isoformat()
        for name in ("polar_baseline", "sail_vmg"):
            await storage.upsert_catalog_entry(
                plugin_name=name,
                co_op_id="coop1",
                state=CO_OP_ACTIVE,
                proposing_boat="fp_a",
                version="1.0.0",
                author="",
                changelog="",
                proposed_at=now,
                resolved_at=now,
                reject_reason=None,
                data_license_gate_passed=1,
            )
        entries = await storage.list_catalog_entries("coop1")
        assert len(entries) == 2
        names = [e["plugin_name"] for e in entries]
        assert "polar_baseline" in names
        assert "sail_vmg" in names

    @pytest.mark.asyncio
    async def test_clear_co_op_default(self, storage: Storage) -> None:
        now = datetime.now(UTC).isoformat()
        await storage.upsert_catalog_entry(
            plugin_name="polar_baseline",
            co_op_id="coop1",
            state=CO_OP_DEFAULT,
            proposing_boat="fp_a",
            version="1.0.0",
            author="",
            changelog="",
            proposed_at=now,
            resolved_at=now,
            reject_reason=None,
            data_license_gate_passed=1,
        )
        await storage.clear_co_op_default("coop1")
        row = await storage.get_catalog_entry("polar_baseline", "coop1")
        assert row is not None
        assert row["state"] == CO_OP_ACTIVE

    @pytest.mark.asyncio
    async def test_mark_plugin_cache_stale(self, storage: Storage) -> None:
        """EARS: version change marks existing cache rows as stale."""
        race_id = await _seed_session(storage)
        cache = AnalysisCache(storage)
        await cache.put(race_id, "polar_baseline", "1.0.0", "hash1", {"metrics": []})

        # Simulate version bump: mark stale for new version "2.0.0"
        count = await storage.mark_plugin_cache_stale("polar_baseline", "2.0.0")
        assert count == 1

        # Cache.get() should now return None (stale)
        result = await cache.get(race_id, "polar_baseline")
        assert result is None

    @pytest.mark.asyncio
    async def test_mark_plugin_cache_stale_same_version_not_affected(
        self, storage: Storage
    ) -> None:
        race_id = await _seed_session(storage)
        cache = AnalysisCache(storage)
        await cache.put(race_id, "polar_baseline", "1.0.0", "hash1", {"metrics": []})

        # Same version — should not mark as stale
        count = await storage.mark_plugin_cache_stale("polar_baseline", "1.0.0")
        assert count == 0

        result = await cache.get(race_id, "polar_baseline")
        assert result is not None


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestCatalogAPI:
    @pytest.mark.asyncio
    async def test_list_catalog_empty(self, storage: Storage) -> None:
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/analysis/catalog?co_op_id=coop1")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_propose_plugin(self, storage: Storage) -> None:
        """Boat owner can propose a plugin when a co-op member."""
        await _seed_user(storage)
        await _seed_co_op(storage, role="member")
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/analysis/catalog/propose",
                json={"plugin_name": "polar_baseline", "co_op_id": "coop1"},
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["state"] == PROPOSED
        assert data["plugin_name"] == "polar_baseline"

    @pytest.mark.asyncio
    async def test_propose_not_member_returns_403(self, storage: Storage) -> None:
        """Decision table: non-member cannot propose."""
        await _seed_user(storage)
        # No co-op membership seeded
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/analysis/catalog/propose",
                json={"plugin_name": "polar_baseline", "co_op_id": "coop1"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_propose_unknown_plugin_returns_404(self, storage: Storage) -> None:
        await _seed_user(storage)
        await _seed_co_op(storage, role="member")
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/analysis/catalog/propose",
                json={"plugin_name": "nonexistent_plugin", "co_op_id": "coop1"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_approve_requires_admin(self, storage: Storage) -> None:
        """Only admin role can approve proposals."""
        await _seed_user(storage)
        await _seed_co_op(storage, role="admin")
        # First propose via catalog state machine
        await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/analysis/catalog/polar_baseline/approve",
                json={"co_op_id": "coop1", "result_sample": _CLEAN_RESULT},
            )
        # Admin user seeded above so this should succeed
        assert resp.status_code == 200
        assert resp.json()["state"] == CO_OP_ACTIVE

    @pytest.mark.asyncio
    async def test_reject_plugin(self, storage: Storage) -> None:
        await _seed_user(storage)
        await _seed_co_op(storage, role="admin")
        await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/analysis/catalog/polar_baseline/reject",
                json={"co_op_id": "coop1", "reason": "Not ready"},
            )
        assert resp.status_code == 200
        assert resp.json()["state"] == REJECTED
        assert resp.json()["reject_reason"] == "Not ready"

    @pytest.mark.asyncio
    async def test_set_default_requires_moderator(self, storage: Storage) -> None:
        await _seed_user(storage)
        await _seed_co_op(storage, role="admin")
        await propose_to_co_op(
            storage, "polar_baseline", "coop1", proposing_boat="fp_a", version="1.0.0"
        )
        await approve(storage, "polar_baseline", "coop1", result_sample=_CLEAN_RESULT)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/analysis/catalog/polar_baseline/set-default",
                json={"co_op_id": "coop1"},
            )
        assert resp.status_code == 200
        assert resp.json()["state"] == CO_OP_DEFAULT


class TestABCompareAPI:
    @pytest.mark.asyncio
    async def test_ab_compare_two_models(self, storage: Storage) -> None:
        """Decision table: own session, two active models → both results returned."""
        race_id = await _seed_session(storage)
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/analysis/ab-compare/{race_id}",
                json={"models": ["polar_baseline", "sail_vmg"]},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == race_id
        assert len(data["panels"]) == 2
        panel_names = [p["plugin_name"] for p in data["panels"]]
        assert "polar_baseline" in panel_names
        assert "sail_vmg" in panel_names

    @pytest.mark.asyncio
    async def test_ab_compare_labels_include_version(self, storage: Storage) -> None:
        race_id = await _seed_session(storage)
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/analysis/ab-compare/{race_id}",
                json={"models": ["polar_baseline", "sail_vmg"]},
            )
        panels = resp.json()["panels"]
        for panel in panels:
            assert "label" in panel
            assert "v" in panel["label"]

    @pytest.mark.asyncio
    async def test_ab_compare_requires_at_least_two_models(self, storage: Storage) -> None:
        race_id = await _seed_session(storage)
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/analysis/ab-compare/{race_id}",
                json={"models": ["polar_baseline"]},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_ab_compare_session_not_found(self, storage: Storage) -> None:
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/analysis/ab-compare/9999",
                json={"models": ["polar_baseline", "sail_vmg"]},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_ab_compare_stale_indicator_included(self, storage: Storage) -> None:
        """EARS: stale_reason appears in panel when cached result is from old version."""
        race_id = await _seed_session(storage)
        await _seed_user(storage)

        # Pre-seed a stale cache entry for polar_baseline
        cache = AnalysisCache(storage)
        await cache.put(race_id, "polar_baseline", "0.1.0", "oldhash", {"metrics": [], "raw": {}})
        # Mark it stale (simulates a version upgrade)
        await storage.mark_plugin_cache_stale("polar_baseline", "1.0.0")

        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/analysis/ab-compare/{race_id}",
                json={"models": ["polar_baseline", "sail_vmg"]},
            )
        assert resp.status_code == 200
        # After A/B compare, polar_baseline should have been re-run (stale_reason cleared)
        panels = resp.json()["panels"]
        polar_panel = next(p for p in panels if p["plugin_name"] == "polar_baseline")
        # After re-run the stale_reason should be None
        assert polar_panel.get("stale_reason") is None


class TestVersionStaleness:
    @pytest.mark.asyncio
    async def test_results_endpoint_includes_stale_reason(self, storage: Storage) -> None:
        """EARS: stale cached results show stale_reason in API response."""
        race_id = await _seed_session(storage)
        await _seed_user(storage)

        # Plant a stale cache entry
        cache = AnalysisCache(storage)
        await cache.put(
            race_id, "polar_baseline", "0.9.0", "oldhash", {"metrics": [], "plugin_name": "polar_baseline", "session_id": race_id, "raw": {}}
        )
        await storage.mark_plugin_cache_stale("polar_baseline", "1.0.0")

        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                f"/api/analysis/results/{race_id}?model=polar_baseline"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("stale_reason") == "version_change"
