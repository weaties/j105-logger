"""Modular storage engine with legacy fallback."""

from __future__ import annotations

from .config import (
    StorageConfig,
    RACE_SLUG_RETENTION_DAYS,
    _SAIL_TYPES,
    _MARK_REFERENCES,
    _CURRENT_VERSION,
    _ts
)
from .legacy import LegacyStorage
from .user import UserRepository
from .session import SessionRepository
from .settings import SettingsRepository, get_effective_setting
from .migration import _MIGRATIONS, _split_migration_sql, apply_migrations

# Re-export types and constants to the top-level helmlog.storage namespace
# This is critical for backward compatibility with tests and other modules.
__all__ = [
    "Storage",
    "StorageConfig",
    "RACE_SLUG_RETENTION_DAYS",
    "get_effective_setting",
    "HeadingRecord",
    "SpeedRecord",
    "DepthRecord",
    "PositionRecord",
    "COGSOGRecord",
    "WindRecord",
    "EnvironmentalRecord",
    "RudderRecord",
    "VideoSession",
    "_ts",
    "_SAIL_TYPES",
    "_MARK_REFERENCES",
    "_CURRENT_VERSION",
    "_MIGRATIONS",
    "_split_migration_sql",
]

# Pull these into the module global scope
from helmlog.nmea2000 import (
    HeadingRecord,
    SpeedRecord,
    DepthRecord,
    PositionRecord,
    COGSOGRecord,
    WindRecord,
    EnvironmentalRecord,
    RudderRecord,
)
from helmlog.video import VideoSession

# Ensure constants are also available at the module level for direct import
_MARK_REFERENCES = _MARK_REFERENCES
_ts = _ts
_SAIL_TYPES = _SAIL_TYPES
_CURRENT_VERSION = _CURRENT_VERSION
_MIGRATIONS = _MIGRATIONS
_split_migration_sql = _split_migration_sql

_POSITIONS: tuple[str, ...] = ("helm", "main", "pit", "bow", "tactician", "guest")


class Storage(LegacyStorage):
    """Refactored Storage engine.
    
    Inherits from LegacyStorage to maintain 100% backward compatibility
    while domain-specific logic is incrementally moved to repositories.
    """

    def __init__(self, config: StorageConfig) -> None:
        # Initialize legacy state
        super().__init__(config)
        
        self.users: UserRepository | None = None
        self.sessions: SessionRepository | None = None
        self.settings: SettingsRepository | None = None
        
        from .engine import DatabaseEngine
        self._engine = DatabaseEngine(self._config.db_path)

    async def connect(self) -> None:
        """Establish connections and initialize repositories."""
        # 1. Open connections using the new Engine
        await self._engine.connect()
        
        # 2. Provide these connections to LegacyStorage BEFORE it connects
        self._db = self._engine.write_conn()
        
        # Match legacy behavior for memory DBs (no read connection)
        if self._config.db_path == ":memory:":
            self._read_db = None
        else:
            self._read_db = self._engine.read_conn()
        
        # 3. Apply migrations using the new runner
        await self.migrate()
        
        # 4. Initialize repositories
        self.users = UserRepository(self._engine)
        self.sessions = SessionRepository(self._engine)
        self.settings = SettingsRepository(self._engine)
        
        # 5. Let LegacyStorage complete its initialization (live cache, etc)
        await super().connect()
        
        # 6. Idempotently seed metadata
        await self._seed_crew_positions_internal()
        await self._seed_controls_internal()
        
        # 7. Override legacy methods with repository implementations
        self.create_user = self.users.create_user
        self.get_user_by_id = self.users.get_user_by_id
        self.get_user_by_email = self.users.get_user_by_email
        self.update_user_role = self.users.update_user_role
        self.update_user_last_seen = self.users.update_user_last_seen
        self.update_user_developer = self.users.update_user_developer
        self.update_user_profile = self.users.update_user_profile
        self.list_users = self.users.list_users
        self.deactivate_user = self.users.deactivate_user
        self.activate_user = self.users.activate_user
        self.delete_user = self.users.delete_user

        self.create_invitation = self.users.create_invitation
        self.get_invitation = self.users.get_invitation
        self.accept_invitation = self.users.accept_invitation
        self.revoke_invitation = self.users.revoke_invitation
        self.list_pending_invitations = self.users.list_pending_invitations
        self.list_pending_invitation_emails = self.users.list_pending_invitation_emails

        self.create_credential = self.users.create_credential
        self.get_credential = self.users.get_credential
        self.get_credential_by_provider_uid = self.users.get_credential_by_provider_uid
        self.update_password_hash = self.users.update_password_hash

        self.create_password_reset_token = self.users.create_password_reset_token
        self.get_password_reset_token = self.users.get_password_reset_token
        self.use_password_reset_token = self.users.use_password_reset_token

        self.create_session = self.users.create_session
        self.get_session = self.users.get_session
        self.delete_session = self.users.delete_session
        self.list_auth_sessions = self.users.list_auth_sessions
        self.delete_expired_sessions = self.users.delete_expired_sessions
        
        self.count_sessions_for_date = self.sessions.count_sessions_for_date
        self.list_event_rules = self.sessions.list_event_rules
        self.get_event_rule = self.sessions.get_event_rule
        self.get_daily_event = self.sessions.get_daily_event
        self.import_race = self.sessions.import_race
        self.get_current_race = self.sessions.get_current_race
        self.list_races_for_date = self.sessions.list_races_for_date
        self.import_synthesized_data = self.sessions.import_synthesized_data
        self.get_scheduled_start = self.sessions.get_scheduled_start
        self.save_synth_wind_params = self.sessions.save_synth_wind_params
        self.save_synth_course_marks = self.sessions.save_synth_course_marks

        self.log_action = self.users.log_action
        self.list_audit_log = self.users.list_audit_log

        self.create_tag = self.users.create_tag
        self.get_tag_by_name = self.users.get_tag_by_name
        self.list_tags = self.users.list_tags
        self.delete_tag = self.users.delete_tag
        
        self.get_setting = self.settings.get_setting
        self.set_setting = self.settings.set_setting
        self.delete_setting = self.settings.delete_setting
        self.create_boat_settings = self.settings.create_boat_settings
        self.add_control = self.settings.add_control

    async def migrate(self) -> None:
        """Apply migrations using the new runner."""
        await apply_migrations(self._engine)

    async def close(self) -> None:
        """Close connections via engine."""
        await super().close()
        await self._engine.close()

    async def _seed_crew_positions_internal(self) -> None:
        """Idempotently seed the crew_positions table."""
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime
        now = _datetime.now(_UTC).isoformat()
        db = self._db
        for order, name in enumerate(_POSITIONS):
            await db.execute(
                "INSERT OR IGNORE INTO crew_positions (name, display_order, created_at)"
                " VALUES (?, ?, ?)",
                (name, order, now),
            )
        await db.commit()

    async def _seed_controls_internal(self) -> None:
        """Internal helper to seed controls from boat_settings."""
        import json as _json
        from helmlog.boat_settings import PARAMETERS, WEIGHT_DISTRIBUTION_PRESETS
        
        db = self._db
        cur = await db.execute("SELECT COUNT(*) FROM controls")
        row = await cur.fetchone()
        if row and row[0] > 0:
            return

        for order, p in enumerate(PARAMETERS):
            preset_json = None
            if p.name == "weight_distribution":
                preset_json = _json.dumps(list(WEIGHT_DISTRIBUTION_PRESETS))
            await db.execute(
                "INSERT OR IGNORE INTO controls"
                " (name, label, unit, input_type, category, sort_order, preset_values)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (p.name, p.label, p.unit, p.input_type, p.category, order, preset_json),
            )
        await db.commit()
