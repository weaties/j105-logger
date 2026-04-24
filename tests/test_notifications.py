"""Tests for moment notifications (#662, was threaded comments #284).

Covers @mention parsing, notification create/list/read/dismiss, the helper
functions that fan notifications out on moment lifecycle events, and the
API endpoints that expose the notification inbox.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio

from helmlog.notifications import (
    notify_mention,
    notify_new_moment,
    notify_reply,
    notify_resolved,
    parse_mentions,
    render_mentions_html,
)
from helmlog.storage import Storage, StorageConfig
from helmlog.web import create_app


@pytest_asyncio.fixture
async def storage() -> Storage:  # type: ignore[misc]
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


async def _seed_users(storage: Storage) -> list[int]:
    u1 = await storage.create_user("alice@example.com", "Alice", "crew")
    u2 = await storage.create_user("bob@example.com", "Bob", "crew")
    u3 = await storage.create_user("charlie@example.com", "Charlie", "viewer")
    return [u1, u2, u3]


async def _seed_session(storage: Storage) -> int:
    race = await storage.start_race(
        "Test",
        datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC),
        "2024-06-15",
        1,
        "Test Race 1",
        "race",
    )
    await storage.end_race(race.id, datetime(2024, 6, 15, 12, 5, 0, tzinfo=UTC))
    return race.id


async def _seed_moment(storage: Storage, session_id: int, user_id: int) -> int:
    return await storage.create_moment(
        session_id=session_id,
        anchor_kind="session",
        subject="Test moment",
        user_id=user_id,
    )


class TestMentionParsing:
    def test_parse_single_mention(self) -> None:
        assert parse_mentions("Hey @Alice check this") == ["Alice"]

    def test_parse_multiple_mentions(self) -> None:
        result = parse_mentions("@Alice and @Bob look at this @Charlie")
        assert result == ["Alice", "Bob", "Charlie"]

    def test_parse_no_mentions(self) -> None:
        assert parse_mentions("No mentions here") == []

    def test_parse_duplicate_mentions(self) -> None:
        result = parse_mentions("@Alice and @Alice again")
        assert result == ["Alice"]

    def test_parse_mention_with_dots(self) -> None:
        result = parse_mentions("Hey @J.Smith check this")
        assert result == ["J.Smith"]

    def test_parse_mention_at_start(self) -> None:
        assert parse_mentions("@Alice") == ["Alice"]

    def test_render_known_mention(self) -> None:
        html = render_mentions_html("Hey @Alice", {"Alice": 1})
        assert '<a class="mention" data-user-id="1">@Alice</a>' in html

    def test_render_unknown_mention(self) -> None:
        html = render_mentions_html("Hey @Unknown", {"Alice": 1})
        assert "@Unknown" in html
        assert "mention" not in html

    def test_render_mixed_mentions(self) -> None:
        html = render_mentions_html("@Alice and @Unknown", {"Alice": 1})
        assert '<a class="mention"' in html
        assert "@Unknown" in html

    def test_parse_multiword_name(self) -> None:
        result = parse_mentions(
            "Hey @dan weatbrook check this", known_names=["dan weatbrook", "Alice"]
        )
        assert result == ["dan weatbrook"]

    def test_parse_multiword_and_single(self) -> None:
        result = parse_mentions(
            "@dan weatbrook and @Alice",
            known_names=["dan weatbrook", "Alice"],
        )
        assert "dan weatbrook" in result
        assert "Alice" in result

    def test_render_multiword_mention(self) -> None:
        html = render_mentions_html("Hey @dan weatbrook", {"dan weatbrook": 1})
        assert "@dan weatbrook</a>" in html
        assert 'data-user-id="1"' in html

    def test_parse_multiword_no_known_names_fallback(self) -> None:
        result = parse_mentions("Hey @dan weatbrook")
        assert result == ["dan"]


class TestNotificationCreation:
    @pytest.mark.asyncio
    async def test_create_notification(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        nid = await storage.create_notification(
            users[0],
            "mention",
            source_moment_id=None,
            session_id=None,
            actor_id=users[1],
            message="mentioned you",
        )
        assert nid > 0

    @pytest.mark.asyncio
    async def test_get_notifications(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        await storage.create_notification(users[0], "mention", actor_id=users[1], message="test 1")
        await storage.create_notification(users[0], "reply", actor_id=users[2], message="test 2")
        notifs = await storage.get_notifications(users[0])
        assert len(notifs) == 2
        assert any(n["actor_name"] is not None for n in notifs)

    @pytest.mark.asyncio
    async def test_get_notifications_unread_only(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        nid = await storage.create_notification(
            users[0], "mention", actor_id=users[1], message="test"
        )
        await storage.mark_notification_read(nid, users[0])
        notifs = await storage.get_notifications(users[0], unread_only=True)
        assert len(notifs) == 0

    @pytest.mark.asyncio
    async def test_notification_count(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        await storage.create_notification(users[0], "mention", actor_id=users[1], message="m1")
        await storage.create_notification(users[0], "reply", actor_id=users[2], message="r1")
        counts = await storage.get_notification_count(users[0])
        assert counts["unread"] == 2
        assert counts["mentions"] == 1

    @pytest.mark.asyncio
    async def test_mark_all_read(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        await storage.create_notification(users[0], "mention", message="m1")
        await storage.create_notification(users[0], "reply", message="r1")
        count = await storage.mark_all_notifications_read(users[0])
        assert count == 2
        counts = await storage.get_notification_count(users[0])
        assert counts["unread"] == 0

    @pytest.mark.asyncio
    async def test_dismiss_notification(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        nid = await storage.create_notification(users[0], "mention", message="m1")
        ok = await storage.dismiss_notification(nid, users[0])
        assert ok is True
        notifs = await storage.get_notifications(users[0])
        assert len(notifs) == 0


class TestNotifyHelpers:
    @pytest.mark.asyncio
    async def test_notify_mention(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        session_id = await _seed_session(storage)
        moment_id = await _seed_moment(storage, session_id, users[0])
        comment_id = await storage.create_comment(moment_id, users[0], "Hey @Bob")

        count = await notify_mention(
            storage, comment_id, moment_id, session_id, users[0], [users[1], users[2]]
        )
        assert count == 2

        notifs = await storage.get_notifications(users[1])
        assert len(notifs) == 1
        assert notifs[0]["type"] == "mention"

    @pytest.mark.asyncio
    async def test_notify_mention_skips_actor(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        session_id = await _seed_session(storage)
        moment_id = await _seed_moment(storage, session_id, users[0])
        comment_id = await storage.create_comment(moment_id, users[0], "Hey @Alice")

        count = await notify_mention(
            storage, comment_id, moment_id, session_id, users[0], [users[0]]
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_notify_new_moment(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        session_id = await _seed_session(storage)
        moment_id = await _seed_moment(storage, session_id, users[0])

        count = await notify_new_moment(storage, moment_id, session_id, users[0])
        assert count >= 2

    @pytest.mark.asyncio
    async def test_notify_reply(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        session_id = await _seed_session(storage)
        moment_id = await _seed_moment(storage, session_id, users[0])
        comment_id = await storage.create_comment(moment_id, users[1], "I agree")

        count = await notify_reply(storage, comment_id, moment_id, session_id, users[1])
        assert count >= 2

    @pytest.mark.asyncio
    async def test_notify_resolved(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        session_id = await _seed_session(storage)
        moment_id = await _seed_moment(storage, session_id, users[0])

        count = await notify_resolved(storage, moment_id, session_id, users[0])
        assert count >= 2


class TestNotificationPreferences:
    @pytest.mark.asyncio
    async def test_set_and_get_preferences(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        await storage.set_notification_preference(
            users[0],
            "session",
            "mention",
            "platform",
            enabled=True,
            frequency="immediate",
        )
        prefs = await storage.get_notification_preferences(users[0])
        assert len(prefs) == 1
        assert prefs[0]["type"] == "mention"
        assert prefs[0]["enabled"] == 1

    @pytest.mark.asyncio
    async def test_disable_notification_type(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        await storage.set_notification_preference(
            users[0],
            "session",
            "new_moment",
            "platform",
            enabled=False,
        )
        eligible = await storage.get_users_for_notification(1, "new_moment")
        user_ids = [u["id"] for u in eligible]
        assert users[0] not in user_ids

    @pytest.mark.asyncio
    async def test_upsert_preference(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        await storage.set_notification_preference(
            users[0],
            "session",
            "mention",
            "platform",
            enabled=True,
        )
        await storage.set_notification_preference(
            users[0],
            "session",
            "mention",
            "platform",
            enabled=False,
        )
        prefs = await storage.get_notification_preferences(users[0])
        assert len(prefs) == 1
        assert prefs[0]["enabled"] == 0


class TestRedactionCascade:
    @pytest.mark.asyncio
    async def test_cascade_redaction(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        await storage.create_notification(
            users[1],
            "mention",
            actor_id=users[0],
            message="hey bob",
        )
        await storage.create_notification(
            users[2],
            "reply",
            actor_id=users[0],
            message="replied to charlie",
        )
        count = await storage.cascade_crew_redaction_to_notifications(users[0])
        assert count == 2

        notifs = await storage.get_notifications(users[1])
        assert notifs[0]["actor_id"] is None
        assert notifs[0]["message"] is None


class TestResolveUserNames:
    @pytest.mark.asyncio
    async def test_resolve_known_names(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        result = await storage.resolve_user_names(["Alice", "Bob"])
        assert result["Alice"] == users[0]
        assert result["Bob"] == users[1]

    @pytest.mark.asyncio
    async def test_resolve_unknown_names(self, storage: Storage) -> None:
        await _seed_users(storage)
        result = await storage.resolve_user_names(["Unknown"])
        assert "Unknown" not in result

    @pytest.mark.asyncio
    async def test_resolve_empty_list(self, storage: Storage) -> None:
        result = await storage.resolve_user_names([])
        assert result == {}


class TestNotificationAPI:
    @pytest.mark.asyncio
    async def test_get_notifications(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        await storage.create_notification(users[0], "mention", message="test")
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/notifications")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_notification_count_api(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        await storage.create_notification(users[0], "mention", message="test")
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/notifications/count")
        assert resp.status_code == 200
        data = resp.json()
        assert "unread" in data
        assert "mentions" in data

    @pytest.mark.asyncio
    async def test_mark_read_api(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        nid = await storage.create_notification(users[0], "mention", message="test")
        ok = await storage.mark_notification_read(nid, users[0])
        assert ok is True

    @pytest.mark.asyncio
    async def test_mark_all_read_api(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        await storage.create_notification(users[0], "mention", message="test")
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/notifications/read-all")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_dismiss_api(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        nid = await storage.create_notification(users[0], "mention", message="test")
        ok = await storage.dismiss_notification(nid, users[0])
        assert ok is True

    @pytest.mark.asyncio
    async def test_preferences_api(self, storage: Storage) -> None:
        users = await _seed_users(storage)
        prefs = await storage.get_notification_preferences(users[0])
        assert prefs == []

        await storage.set_notification_preference(
            users[0],
            "session",
            "mention",
            "platform",
        )
        prefs = await storage.get_notification_preferences(users[0])
        assert len(prefs) == 1

    @pytest.mark.asyncio
    async def test_preferences_api_endpoint_get(self, storage: Storage) -> None:
        await _seed_users(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/notifications/preferences")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_attention_page(self, storage: Storage) -> None:
        await _seed_users(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/attention")
        assert resp.status_code == 200
        assert "Notifications" in resp.text

    @pytest.mark.asyncio
    async def test_moment_creates_notification(self, storage: Storage) -> None:
        """Creating a moment via API should fire new_moment notifications."""
        users = await _seed_users(storage)
        session_id = await _seed_session(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/sessions/{session_id}/moments",
                json={"anchor_kind": "session", "subject": "Test moment"},
            )
        assert resp.status_code == 201

        notifs = await storage.get_notifications(users[1])
        assert any(n["type"] == "new_moment" for n in notifs)

    @pytest.mark.asyncio
    async def test_comment_creates_reply_notification(self, storage: Storage) -> None:
        """Creating a comment via API should fire reply (and mention) notifications."""
        users = await _seed_users(storage)
        session_id = await _seed_session(storage)
        moment_id = await _seed_moment(storage, session_id, users[0])
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/moments/{moment_id}/comments",
                json={"body": "Hey @Bob check this out"},
            )
        assert resp.status_code == 201

        notifs = await storage.get_notifications(users[1])
        types = {n["type"] for n in notifs}
        assert "mention" in types
        assert "reply" in types

    @pytest.mark.asyncio
    async def test_resolve_creates_notification(self, storage: Storage) -> None:
        """Resolving a moment via API should fire resolved notifications."""
        users = await _seed_users(storage)
        session_id = await _seed_session(storage)
        moment_id = await _seed_moment(storage, session_id, users[0])
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/moments/{moment_id}/resolve",
                json={"resolution_summary": "Done"},
            )
        assert resp.status_code == 200

        notifs = await storage.get_notifications(users[1])
        assert any(n["type"] == "resolved" for n in notifs)
