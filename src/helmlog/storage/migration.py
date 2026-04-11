"""Schema versioning and migrations."""

from __future__ import annotations

from typing import TYPE_CHECKING
from loguru import logger

if TYPE_CHECKING:
    from .engine import DatabaseEngine

_CURRENT_VERSION: int = 60

_MIGRATIONS: dict[int, str] = {
    1: """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS headings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            source_addr INTEGER NOT NULL,
            heading_deg REAL    NOT NULL,
            deviation_deg REAL,
            variation_deg REAL
        );

        CREATE TABLE IF NOT EXISTS speeds (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            source_addr INTEGER NOT NULL,
            speed_kts   REAL    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS depths (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            source_addr INTEGER NOT NULL,
            depth_m     REAL    NOT NULL,
            offset_m    REAL
        );

        CREATE TABLE IF NOT EXISTS positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT    NOT NULL,
            source_addr   INTEGER NOT NULL,
            latitude_deg  REAL    NOT NULL,
            longitude_deg REAL    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cogsog (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            source_addr INTEGER NOT NULL,
            cog_deg     REAL    NOT NULL,
            sog_kts     REAL    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS winds (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL,
            source_addr     INTEGER NOT NULL,
            wind_speed_kts  REAL    NOT NULL,
            wind_angle_deg  REAL    NOT NULL,
            reference       INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS environmental (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT    NOT NULL,
            source_addr   INTEGER NOT NULL,
            water_temp_c  REAL    NOT NULL
        );
    """,
    2: """
        CREATE INDEX IF NOT EXISTS idx_headings_ts     ON headings(ts);
        CREATE INDEX IF NOT EXISTS idx_speeds_ts       ON speeds(ts);
        CREATE INDEX IF NOT EXISTS idx_depths_ts       ON depths(ts);
        CREATE INDEX IF NOT EXISTS idx_positions_ts    ON positions(ts);
        CREATE INDEX IF NOT EXISTS idx_cogsog_ts       ON cogsog(ts);
        CREATE INDEX IF NOT EXISTS idx_winds_ts        ON winds(ts);
        CREATE INDEX IF NOT EXISTS idx_environmental_ts ON environmental(ts);
    """,
    3: """
        CREATE TABLE IF NOT EXISTS video_sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            url           TEXT    NOT NULL,
            video_id      TEXT    NOT NULL,
            title         TEXT    NOT NULL,
            duration_s    REAL    NOT NULL,
            sync_utc      TEXT    NOT NULL,
            sync_offset_s REAL    NOT NULL,
            created_at    TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_video_sessions_sync_utc ON video_sessions(sync_utc);
    """,
    4: """
        CREATE TABLE IF NOT EXISTS weather (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL,
            lat             REAL    NOT NULL,
            lon             REAL    NOT NULL,
            wind_speed_kts  REAL    NOT NULL,
            wind_dir_deg    REAL    NOT NULL,
            air_temp_c      REAL    NOT NULL,
            pressure_hpa    REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_weather_ts ON weather(ts);
    """,
    5: """
        CREATE TABLE IF NOT EXISTS tides (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT    NOT NULL,
            station_id   TEXT    NOT NULL,
            station_name TEXT    NOT NULL,
            height_m     REAL    NOT NULL,
            type         TEXT    NOT NULL,
            UNIQUE(ts, station_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tides_ts ON tides(ts);
    """,
    6: """
        CREATE TABLE IF NOT EXISTS audio_sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path    TEXT    NOT NULL,
            device_name  TEXT    NOT NULL,
            start_utc    TEXT    NOT NULL,
            end_utc      TEXT,
            sample_rate  INTEGER NOT NULL,
            channels     INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audio_sessions_start_utc ON audio_sessions(start_utc);
    """,
    7: """
        CREATE TABLE IF NOT EXISTS races (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE,
            event       TEXT    NOT NULL,
            race_num    INTEGER NOT NULL,
            date        TEXT    NOT NULL,
            start_utc   TEXT    NOT NULL,
            end_utc     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_races_date      ON races(date);
        CREATE INDEX IF NOT EXISTS idx_races_start_utc ON races(start_utc);

        CREATE TABLE IF NOT EXISTS daily_events (
            date        TEXT    PRIMARY KEY,
            event_name  TEXT    NOT NULL
        );
    """,
    8: """
        ALTER TABLE races ADD COLUMN session_type TEXT NOT NULL DEFAULT 'race';
    """,
    9: """
        ALTER TABLE audio_sessions ADD COLUMN race_id INTEGER REFERENCES races(id);
        ALTER TABLE audio_sessions ADD COLUMN session_type TEXT NOT NULL DEFAULT 'race';
        ALTER TABLE audio_sessions ADD COLUMN name TEXT;
    """,
    10: """
        CREATE TABLE IF NOT EXISTS race_crew (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id   INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            position  TEXT    NOT NULL,
            sailor    TEXT    NOT NULL,
            UNIQUE(race_id, position)
        );
        CREATE INDEX IF NOT EXISTS idx_race_crew_race_id ON race_crew(race_id);

        CREATE TABLE IF NOT EXISTS recent_sailors (
            sailor    TEXT PRIMARY KEY,
            last_used TEXT NOT NULL
        );
    """,
    11: """
        CREATE TABLE IF NOT EXISTS boats (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sail_number TEXT UNIQUE NOT NULL,
            name        TEXT,
            class       TEXT,
            last_used   TEXT
        );
        CREATE TABLE IF NOT EXISTS race_results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id     INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            place       INTEGER NOT NULL,
            boat_id     INTEGER NOT NULL REFERENCES boats(id),
            finish_time TEXT,
            dnf         INTEGER NOT NULL DEFAULT 0,
            dns         INTEGER NOT NULL DEFAULT 0,
            notes       TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE(race_id, place),
            UNIQUE(race_id, boat_id)
        );
        CREATE INDEX IF NOT EXISTS idx_race_results_race_id ON race_results(race_id);
    """,
    12: """
        CREATE TABLE IF NOT EXISTS session_notes (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id            INTEGER REFERENCES races(id) ON DELETE CASCADE,
            audio_session_id   INTEGER REFERENCES audio_sessions(id) ON DELETE CASCADE,
            ts                 TEXT NOT NULL,
            note_type          TEXT NOT NULL DEFAULT 'text',
            body               TEXT,
            photo_path         TEXT,
            created_at         TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_session_notes_race_id
            ON session_notes(race_id);
        CREATE INDEX IF NOT EXISTS idx_session_notes_ts
            ON session_notes(ts);
    """,
    13: """
        CREATE TABLE IF NOT EXISTS race_videos (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id          INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            youtube_url      TEXT NOT NULL,
            video_id         TEXT NOT NULL,
            label            TEXT NOT NULL DEFAULT '',
            sync_utc         TEXT NOT NULL,
            sync_offset_s    REAL NOT NULL DEFAULT 0,
            duration_s       REAL,
            title            TEXT NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_race_videos_race_id
            ON race_videos(race_id);
    """,
    14: """
        CREATE TABLE IF NOT EXISTS sails (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            type    TEXT    NOT NULL,
            name    TEXT    NOT NULL,
            notes   TEXT,
            active  INTEGER NOT NULL DEFAULT 1,
            UNIQUE(type, name)
        );

        CREATE TABLE IF NOT EXISTS race_sails (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id  INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            sail_id  INTEGER NOT NULL REFERENCES sails(id),
            UNIQUE(race_id, sail_id)
        );
        CREATE INDEX IF NOT EXISTS idx_race_sails_race_id ON race_sails(race_id);

        CREATE TABLE IF NOT EXISTS sail_defaults (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            sail_id INTEGER NOT NULL REFERENCES sails(id) ON DELETE CASCADE,
            UNIQUE(sail_id)
        );

        CREATE TABLE IF NOT EXISTS sail_changes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id      INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            ts           TEXT NOT NULL,
            main_id      INTEGER REFERENCES sails(id),
            jib_id       INTEGER REFERENCES sails(id),
            spinnaker_id INTEGER REFERENCES sails(id)
        );
        CREATE INDEX IF NOT EXISTS idx_sail_changes_race_ts ON sail_changes(race_id, ts);
    """,
    15: """
        CREATE TABLE IF NOT EXISTS transcripts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            audio_session_id INTEGER NOT NULL REFERENCES audio_sessions(id) ON DELETE CASCADE,
            status           TEXT    NOT NULL DEFAULT 'pending',
            text             TEXT,
            error_msg        TEXT,
            model            TEXT,
            created_utc      TEXT    NOT NULL,
            updated_utc      TEXT    NOT NULL,
            UNIQUE(audio_session_id)
        );
        CREATE INDEX IF NOT EXISTS idx_transcripts_audio_session_id
            ON transcripts(audio_session_id);
    """,
    16: """
        ALTER TABLE transcripts ADD COLUMN segments_json TEXT;
    """,
    17: """
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT UNIQUE NOT NULL,
            name       TEXT,
            role       TEXT NOT NULL DEFAULT 'viewer',
            created_at TEXT NOT NULL,
            last_seen  TEXT
        );

        CREATE TABLE IF NOT EXISTS invite_tokens (
            token      TEXT PRIMARY KEY,
            email      TEXT NOT NULL,
            role       TEXT NOT NULL,
            created_by INTEGER REFERENCES users(id),
            expires_at TEXT NOT NULL,
            used_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS auth_sessions (
            session_id TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            ip         TEXT,
            user_agent TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id ON auth_sessions(user_id);

        ALTER TABLE session_notes ADD COLUMN user_id INTEGER REFERENCES users(id);
        ALTER TABLE race_videos   ADD COLUMN user_id INTEGER REFERENCES users(id);
    """,
    18: """
        CREATE TABLE IF NOT EXISTS polar_baseline (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tws_bin       INTEGER NOT NULL,
            twa_bin       INTEGER NOT NULL,
            mean_bsp      REAL    NOT NULL,
            p90_bsp       REAL    NOT NULL,
            session_count INTEGER NOT NULL,
            sample_count  INTEGER NOT NULL,
            built_at      TEXT    NOT NULL,
            UNIQUE(tws_bin, twa_bin)
        );
        CREATE INDEX IF NOT EXISTS idx_polar_tws_twa ON polar_baseline(tws_bin, twa_bin);
    """,
    19: """
        -- Audit log (#93)
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            user_id     INTEGER REFERENCES users(id),
            action      TEXT NOT NULL,
            detail      TEXT,
            ip_address  TEXT,
            user_agent  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);
        CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);

        -- Tags (#99)
        CREATE TABLE IF NOT EXISTS tags (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            color      TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS session_tags (
            session_id INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (session_id, tag_id)
        );

        CREATE TABLE IF NOT EXISTS note_tags (
            note_id    INTEGER NOT NULL REFERENCES session_notes(id) ON DELETE CASCADE,
            tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (note_id, tag_id)
        );

        -- Headshots (#100)
        ALTER TABLE users ADD COLUMN avatar_path TEXT;
    """,
    20: """
        -- Camera session tracking (#98)
        CREATE TABLE IF NOT EXISTS camera_sessions (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id            INTEGER NOT NULL REFERENCES races(id),
            camera_name           TEXT NOT NULL,
            camera_ip             TEXT NOT NULL,
            recording_started_utc TEXT,
            recording_stopped_utc TEXT,
            sync_offset_ms        INTEGER,
            error                 TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_camera_sessions_session
            ON camera_sessions(session_id);
    """,
    21: """
        -- Persistent camera configuration (#147)
        CREATE TABLE IF NOT EXISTS cameras (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name  TEXT NOT NULL UNIQUE,
            ip    TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT 'insta360-x4'
        );
    """,
    22: """
        -- WiFi credentials for camera AP networks (#147)
        ALTER TABLE cameras ADD COLUMN wifi_ssid TEXT;
        ALTER TABLE cameras ADD COLUMN wifi_password TEXT;
    """,
    23: """
        -- Configurable day-of-week event rules (#154)
        CREATE TABLE IF NOT EXISTS event_rules (
            weekday     INTEGER PRIMARY KEY CHECK(weekday BETWEEN 0 AND 6),
            event_name  TEXT NOT NULL
        );
        -- Seed with existing hardcoded defaults
        INSERT OR IGNORE INTO event_rules (weekday, event_name) VALUES (0, 'BallardCup');
        INSERT OR IGNORE INTO event_rules (weekday, event_name) VALUES (2, 'CYC');
    """,
    24: """
        -- Admin-configurable settings (#146)
        CREATE TABLE IF NOT EXISTS app_settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
    """,
    25: """
        -- Gaia GPS backfill (#101): track provenance metadata on races
        ALTER TABLE races ADD COLUMN source TEXT NOT NULL DEFAULT 'live';
        ALTER TABLE races ADD COLUMN source_id TEXT;
        ALTER TABLE races ADD COLUMN imported_at TEXT;
    """,
    26: """
        -- Data-policy compliance (#194–#211)

        -- Crew consent tracking (#202)
        CREATE TABLE IF NOT EXISTS crew_consents (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER REFERENCES users(id),
            consent_type TEXT    NOT NULL,
            granted      INTEGER NOT NULL DEFAULT 1,
            granted_at   TEXT    NOT NULL,
            revoked_at   TEXT,
            UNIQUE(user_id, consent_type)
        );
        CREATE INDEX IF NOT EXISTS idx_crew_consents_user ON crew_consents(user_id);

        -- Transcript speaker anonymization map (#197)
        ALTER TABLE transcripts ADD COLUMN speaker_anon_map TEXT;

        -- FK cascade fixes (#206): recreate tables with proper ON DELETE behavior.
        -- race_results.boat_id → ON DELETE SET NULL
        CREATE TABLE IF NOT EXISTS race_results_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id     INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            place       INTEGER NOT NULL,
            boat_id     INTEGER REFERENCES boats(id) ON DELETE SET NULL,
            finish_time TEXT,
            dnf         INTEGER NOT NULL DEFAULT 0,
            dns         INTEGER NOT NULL DEFAULT 0,
            notes       TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE(race_id, place),
            UNIQUE(race_id, boat_id)
        );
        INSERT INTO race_results_new
            SELECT id, race_id, place, boat_id, finish_time, dnf, dns, notes, created_at
            FROM race_results;
        DROP TABLE race_results;
        ALTER TABLE race_results_new RENAME TO race_results;
        CREATE INDEX IF NOT EXISTS idx_race_results_race_id ON race_results(race_id);

        -- invite_tokens.created_by → ON DELETE SET NULL
        CREATE TABLE IF NOT EXISTS invite_tokens_new (
            token      TEXT PRIMARY KEY,
            email      TEXT NOT NULL,
            role       TEXT NOT NULL,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            expires_at TEXT NOT NULL,
            used_at    TEXT
        );
        INSERT INTO invite_tokens_new
            SELECT token, email, role, created_by, expires_at, used_at
            FROM invite_tokens;
        DROP TABLE invite_tokens;
        ALTER TABLE invite_tokens_new RENAME TO invite_tokens;

        -- session_notes.user_id → ON DELETE SET NULL
        CREATE TABLE IF NOT EXISTS session_notes_new (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id            INTEGER REFERENCES races(id) ON DELETE CASCADE,
            audio_session_id   INTEGER REFERENCES audio_sessions(id) ON DELETE CASCADE,
            ts                 TEXT NOT NULL,
            note_type          TEXT NOT NULL DEFAULT 'text',
            body               TEXT,
            photo_path         TEXT,
            created_at         TEXT NOT NULL,
            user_id            INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
        INSERT INTO session_notes_new
            SELECT id, race_id, audio_session_id, ts, note_type, body,
                   photo_path, created_at, user_id
            FROM session_notes;
        DROP TABLE session_notes;
        ALTER TABLE session_notes_new RENAME TO session_notes;
        CREATE INDEX IF NOT EXISTS idx_session_notes_race_id ON session_notes(race_id);
        CREATE INDEX IF NOT EXISTS idx_session_notes_ts ON session_notes(ts);

        -- race_videos.user_id → ON DELETE SET NULL
        CREATE TABLE IF NOT EXISTS race_videos_new (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id          INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            youtube_url      TEXT NOT NULL,
            video_id         TEXT NOT NULL,
            label            TEXT NOT NULL DEFAULT '',
            sync_utc         TEXT NOT NULL,
            sync_offset_s    REAL NOT NULL DEFAULT 0,
            duration_s       REAL,
            title            TEXT NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL,
            user_id          INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
        INSERT INTO race_videos_new
            SELECT id, race_id, youtube_url, video_id, label, sync_utc,
                   sync_offset_s, duration_s, title, created_at, user_id
            FROM race_videos;
        DROP TABLE race_videos;
        ALTER TABLE race_videos_new RENAME TO race_videos;
        CREATE INDEX IF NOT EXISTS idx_race_videos_race_id ON race_videos(race_id);
    """,
    27: """
        -- Deployment management (#125)
        CREATE TABLE IF NOT EXISTS deployment_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            from_sha   TEXT NOT NULL,
            to_sha     TEXT NOT NULL,
            trigger    TEXT NOT NULL DEFAULT 'manual',
            status     TEXT NOT NULL DEFAULT 'success',
            error      TEXT,
            started_at TEXT NOT NULL,
            user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_deployment_log_started
            ON deployment_log(started_at);
    """,
    28: """
        -- Federation Phase 1: identity, co-op membership, session sharing

        -- This boat's keypair reference (key material in filesystem, not DB)
        CREATE TABLE IF NOT EXISTS boat_identity (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            pub_key     TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            sail_number TEXT NOT NULL,
            boat_name   TEXT,
            created_at  TEXT NOT NULL
        );

        -- Co-ops this boat belongs to
        CREATE TABLE IF NOT EXISTS co_op_memberships (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            co_op_id        TEXT NOT NULL,
            co_op_name      TEXT NOT NULL,
            co_op_pub       TEXT NOT NULL,
            membership_json TEXT NOT NULL,
            role            TEXT NOT NULL DEFAULT 'member',
            joined_at       TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'active',
            UNIQUE(co_op_id)
        );

        -- Per-session co-op sharing decisions
        CREATE TABLE IF NOT EXISTS session_sharing (
            session_id  INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            co_op_id    TEXT NOT NULL,
            shared_at   TEXT NOT NULL,
            shared_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            embargo_until TEXT,
            event_name  TEXT,
            PRIMARY KEY (session_id, co_op_id)
        );

        -- Known peers (other boats in co-ops we belong to)
        CREATE TABLE IF NOT EXISTS co_op_peers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            co_op_id        TEXT NOT NULL,
            boat_pub        TEXT NOT NULL,
            fingerprint     TEXT NOT NULL,
            sail_number     TEXT,
            boat_name       TEXT,
            tailscale_ip    TEXT,
            last_seen       TEXT,
            membership_json TEXT NOT NULL,
            UNIQUE(co_op_id, fingerprint)
        );

        -- Co-op data access audit trail
        CREATE TABLE IF NOT EXISTS co_op_audit (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            co_op_id        TEXT NOT NULL,
            accessor_fp     TEXT NOT NULL,
            action          TEXT NOT NULL,
            resource        TEXT,
            timestamp       TEXT NOT NULL,
            ip              TEXT,
            points_count    INTEGER,
            bytes_transferred INTEGER,
            nonce_hash      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_co_op_audit_ts ON co_op_audit(timestamp);
        CREATE INDEX IF NOT EXISTS idx_co_op_audit_accessor ON co_op_audit(accessor_fp);

        -- Nonce deduplication for replay protection
        CREATE TABLE IF NOT EXISTS request_nonces (
            nonce_hash  TEXT PRIMARY KEY,
            timestamp   TEXT NOT NULL,
            boat_fp     TEXT NOT NULL
        );
    """,
    29: """
        ALTER TABLE races ADD COLUMN peer_fingerprint TEXT;
        ALTER TABLE races ADD COLUMN peer_co_op_id TEXT;
    """,
    30: """
        ALTER TABLE positions ADD COLUMN race_id INTEGER REFERENCES races(id);
        CREATE INDEX IF NOT EXISTS idx_positions_race_id ON positions (race_id);
    """,
    31: """
        -- Maneuver detection (#232)
        CREATE TABLE IF NOT EXISTS maneuvers (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            type         TEXT NOT NULL,
            ts           TEXT NOT NULL,
            end_ts       TEXT,
            duration_sec REAL,
            loss_kts     REAL,
            vmg_loss_kts REAL,
            tws_bin      INTEGER,
            twa_bin      INTEGER,
            details      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_maneuvers_session ON maneuvers(session_id);
        CREATE INDEX IF NOT EXISTS idx_maneuvers_type ON maneuvers(type);
    """,
    32: """
        -- Add race_id to instrument tables so overlapping synthesized sessions
        -- can be distinguished.  Real sailing data has no overlaps, but the
        -- synthesizer generates sessions with overlapping timestamp ranges.
        ALTER TABLE headings ADD COLUMN race_id INTEGER REFERENCES races(id);
        ALTER TABLE speeds   ADD COLUMN race_id INTEGER REFERENCES races(id);
        ALTER TABLE winds    ADD COLUMN race_id INTEGER REFERENCES races(id);
        ALTER TABLE cogsog   ADD COLUMN race_id INTEGER REFERENCES races(id);
        ALTER TABLE depths   ADD COLUMN race_id INTEGER REFERENCES races(id);
        CREATE INDEX IF NOT EXISTS idx_headings_race_id ON headings(race_id);
        CREATE INDEX IF NOT EXISTS idx_speeds_race_id   ON speeds(race_id);
        CREATE INDEX IF NOT EXISTS idx_winds_race_id    ON winds(race_id);
        CREATE INDEX IF NOT EXISTS idx_cogsog_race_id   ON cogsog(race_id);
        CREATE INDEX IF NOT EXISTS idx_depths_race_id   ON depths(race_id);
    """,
    33: """
        -- Wind field parameters and course marks for synthesized sessions (#248)
        CREATE TABLE IF NOT EXISTS synth_wind_params (
            session_id        INTEGER PRIMARY KEY REFERENCES races(id) ON DELETE CASCADE,
            seed              INTEGER NOT NULL,
            base_twd          REAL NOT NULL,
            tws_low           REAL NOT NULL,
            tws_high          REAL NOT NULL,
            shift_interval_lo REAL NOT NULL,
            shift_interval_hi REAL NOT NULL,
            shift_magnitude_lo REAL NOT NULL,
            shift_magnitude_hi REAL NOT NULL,
            ref_lat           REAL NOT NULL,
            ref_lon           REAL NOT NULL,
            duration_s        REAL NOT NULL,
            course_type       TEXT NOT NULL,
            leg_distance_nm   REAL,
            laps              INTEGER,
            mark_sequence     TEXT
        );
        CREATE TABLE IF NOT EXISTS synth_course_marks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            mark_key    TEXT NOT NULL,
            mark_name   TEXT NOT NULL,
            lat         REAL NOT NULL,
            lon         REAL NOT NULL,
            UNIQUE(session_id, mark_key)
        );
        CREATE INDEX IF NOT EXISTS idx_synth_course_marks_session
            ON synth_course_marks(session_id);
    """,
    34: """
        ALTER TABLE users ADD COLUMN is_developer INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE invite_tokens ADD COLUMN is_developer INTEGER NOT NULL DEFAULT 0;
    """,
    35: """
        -- Migration 35: Invitation + flexible auth (#268)
        CREATE TABLE IF NOT EXISTS user_credentials (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider      TEXT NOT NULL,
            provider_uid  TEXT,
            password_hash TEXT,
            created_at    TEXT NOT NULL,
            UNIQUE(user_id, provider)
        );
        CREATE INDEX IF NOT EXISTS idx_user_credentials_provider
            ON user_credentials(provider, provider_uid);

        CREATE TABLE IF NOT EXISTS invitations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            token       TEXT UNIQUE NOT NULL,
            email       TEXT NOT NULL,
            role        TEXT NOT NULL,
            name        TEXT,
            is_developer INTEGER NOT NULL DEFAULT 0,
            invited_by  INTEGER REFERENCES users(id),
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            accepted_at TEXT,
            revoked_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            token      TEXT UNIQUE NOT NULL,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL,
            used_at    TEXT
        );

        ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;

        DROP TABLE IF EXISTS invite_tokens;
    """,
    36: """
        CREATE TABLE IF NOT EXISTS boat_settings (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id          INTEGER REFERENCES races(id) ON DELETE CASCADE,
            ts               TEXT NOT NULL,
            parameter        TEXT NOT NULL,
            value            TEXT NOT NULL,
            source           TEXT NOT NULL,
            extraction_run_id INTEGER,
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_boat_settings_race_ts
            ON boat_settings(race_id, ts);
        CREATE INDEX IF NOT EXISTS idx_boat_settings_extraction_run
            ON boat_settings(extraction_run_id);
    """,
    37: """
        CREATE TABLE IF NOT EXISTS comment_threads (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id         INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            anchor_timestamp   TEXT,
            mark_reference     TEXT,
            title              TEXT,
            created_by         INTEGER REFERENCES users(id),
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL,
            resolved           INTEGER NOT NULL DEFAULT 0,
            resolved_at        TEXT,
            resolved_by        INTEGER REFERENCES users(id),
            resolution_summary TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_comment_threads_session
            ON comment_threads(session_id);

        CREATE TABLE IF NOT EXISTS comments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id  INTEGER NOT NULL REFERENCES comment_threads(id) ON DELETE CASCADE,
            author     INTEGER REFERENCES users(id),
            body       TEXT NOT NULL,
            created_at TEXT NOT NULL,
            edited_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_comments_thread
            ON comments(thread_id);

        CREATE TABLE IF NOT EXISTS comment_read_state (
            user_id   INTEGER REFERENCES users(id) ON DELETE CASCADE,
            thread_id INTEGER NOT NULL REFERENCES comment_threads(id) ON DELETE CASCADE,
            last_read TEXT NOT NULL,
            PRIMARY KEY (user_id, thread_id)
        );
    """,
    38: """
        -- Crew management overhaul (#305)

        -- 1. Add weight column to users
        ALTER TABLE users ADD COLUMN weight_lbs REAL;

        -- 2. Configurable crew positions (replaces hardcoded _POSITIONS)
        CREATE TABLE IF NOT EXISTS crew_positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL UNIQUE,
            display_order INTEGER NOT NULL,
            created_at    TEXT NOT NULL
        );

        -- 3. Two-tier crew defaults (boat-level + race-level)
        CREATE TABLE IF NOT EXISTS crew_defaults (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id       INTEGER REFERENCES races(id) ON DELETE CASCADE,
            position_id   INTEGER NOT NULL REFERENCES crew_positions(id),
            user_id       INTEGER REFERENCES users(id),
            attributed    INTEGER NOT NULL DEFAULT 1,
            body_weight   REAL,
            gear_weight   REAL,
            created_at    TEXT NOT NULL,
            UNIQUE(race_id, position_id)
        );
        CREATE INDEX IF NOT EXISTS idx_crew_defaults_race ON crew_defaults(race_id);

        -- 4. Add user_id column to crew_consents
        ALTER TABLE crew_consents ADD COLUMN user_id INTEGER REFERENCES users(id);
    """,
    39: """
        -- Point-of-sail field for sail inventory (#308)
        ALTER TABLE sails ADD COLUMN point_of_sail TEXT NOT NULL DEFAULT 'both';
        UPDATE sails SET point_of_sail = 'upwind' WHERE type = 'jib';
        UPDATE sails SET point_of_sail = 'downwind' WHERE type = 'spinnaker';
    """,
    40: """
        -- Default sail selection — boat-level defaults (#306)
        CREATE TABLE IF NOT EXISTS sail_defaults (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            sail_id INTEGER NOT NULL REFERENCES sails(id) ON DELETE CASCADE,
            UNIQUE(sail_id)
        );
    """,
    41: """
        -- Timestamped sail changes (#311)
        CREATE TABLE IF NOT EXISTS sail_changes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id      INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            ts           TEXT NOT NULL,
            main_id      INTEGER REFERENCES sails(id),
            jib_id       INTEGER REFERENCES sails(id),
            spinnaker_id INTEGER REFERENCES sails(id)
        );
        CREATE INDEX IF NOT EXISTS idx_sail_changes_race_ts ON sail_changes(race_id, ts);
    """,
    42: """
        -- Pluggable analysis framework (#283)
        CREATE TABLE IF NOT EXISTS analysis_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            plugin_name TEXT NOT NULL,
            plugin_version TEXT NOT NULL,
            data_hash TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, plugin_name)
        );
        CREATE INDEX IF NOT EXISTS idx_analysis_cache_session
            ON analysis_cache(session_id);

        CREATE TABLE IF NOT EXISTS analysis_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            scope_id TEXT,
            model_name TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(scope, scope_id)
        );
    """,
    43: """
        -- Threaded comments Phase 2 — notifications (#284)
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            type TEXT NOT NULL,
            source_thread_id INTEGER REFERENCES comment_threads(id) ON DELETE CASCADE,
            source_comment_id INTEGER REFERENCES comments(id) ON DELETE SET NULL,
            session_id INTEGER REFERENCES races(id) ON DELETE CASCADE,
            actor_id INTEGER REFERENCES users(id),
            message TEXT,
            created_at TEXT NOT NULL,
            read INTEGER NOT NULL DEFAULT 0,
            dismissed INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_user
            ON notifications(user_id, read);
        CREATE INDEX IF NOT EXISTS idx_notifications_session
            ON notifications(session_id);

        CREATE TABLE IF NOT EXISTS notification_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            scope TEXT NOT NULL,
            type TEXT NOT NULL,
            channel TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            frequency TEXT NOT NULL DEFAULT 'immediate',
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, scope, type, channel)
        );
    """,
    44: """
        -- Co-op session matching (#281)
        ALTER TABLE races ADD COLUMN shared_name TEXT;
        ALTER TABLE races ADD COLUMN match_group_id TEXT;
        ALTER TABLE races ADD COLUMN match_confirmed INTEGER DEFAULT 0;

        CREATE TABLE IF NOT EXISTS session_match_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_group_id TEXT NOT NULL,
            proposer_fingerprint TEXT NOT NULL,
            local_session_id INTEGER REFERENCES races(id) ON DELETE CASCADE,
            peer_session_id INTEGER,
            centroid_lat REAL,
            centroid_lon REAL,
            start_utc TEXT,
            end_utc TEXT,
            status TEXT NOT NULL DEFAULT 'candidate',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_smp_match_group
            ON session_match_proposals(match_group_id);
        CREATE INDEX IF NOT EXISTS idx_smp_status
            ON session_match_proposals(status);

        CREATE TABLE IF NOT EXISTS session_match_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_group_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            confirmed_at TEXT NOT NULL,
            UNIQUE(match_group_id, fingerprint)
        );

        CREATE TABLE IF NOT EXISTS session_match_names (
            match_group_id TEXT PRIMARY KEY,
            shared_name TEXT NOT NULL,
            proposed_by TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """,
    45: """
        -- Cache session centroids on races table to avoid N+1 queries (#session-matching)
        ALTER TABLE races ADD COLUMN centroid_lat REAL;
        ALTER TABLE races ADD COLUMN centroid_lon REAL;
    """,
    46: """
        -- Scheduled race starts (#345)
        CREATE TABLE IF NOT EXISTS scheduled_starts (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            scheduled_start_utc TEXT    NOT NULL,
            event               TEXT    NOT NULL,
            session_type        TEXT    NOT NULL DEFAULT 'race',
            created_at          TEXT    NOT NULL
        );
    """,
    47: """
        -- Customizable color schemes (#347)
        ALTER TABLE users ADD COLUMN color_scheme TEXT;
        CREATE TABLE IF NOT EXISTS color_schemes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            bg          TEXT    NOT NULL,
            text_color  TEXT    NOT NULL,
            accent      TEXT    NOT NULL,
            created_by  INTEGER REFERENCES users(id),
            created_at  TEXT    NOT NULL
        );
    """,
    48: """
        -- Tuning extraction from transcripts (#276)
        CREATE TABLE IF NOT EXISTS extraction_runs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            transcript_id  INTEGER NOT NULL REFERENCES transcripts(id),
            method         TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'created',
            item_count     INTEGER NOT NULL DEFAULT 0,
            accepted_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_extraction_runs_transcript
            ON extraction_runs(transcript_id);

        CREATE TABLE IF NOT EXISTS extraction_items (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            extraction_run_id INTEGER NOT NULL REFERENCES extraction_runs(id) ON DELETE CASCADE,
            parameter_name    TEXT NOT NULL,
            extracted_value   REAL NOT NULL,
            segment_start     REAL NOT NULL,
            segment_end       REAL NOT NULL,
            segment_text      TEXT NOT NULL,
            confidence        REAL NOT NULL DEFAULT 1.0,
            status            TEXT NOT NULL DEFAULT 'pending',
            reviewed_at       TEXT,
            reviewed_by       INTEGER REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_extraction_items_run
            ON extraction_items(extraction_run_id);
    """,
    49: """
        -- Pluggable visualization framework (#286)
        CREATE TABLE IF NOT EXISTS visualization_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            scope_id TEXT,
            plugin_names TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(scope, scope_id)
        );

        CREATE TABLE IF NOT EXISTS visualization_selections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_id INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            plugin_names TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, session_id)
        );
    """,
    50: """
        -- WLAN profile management (#256)
        CREATE TABLE IF NOT EXISTS wlan_profiles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            ssid        TEXT NOT NULL,
            password    TEXT,
            is_default  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        );
    """,
    51: """
        -- Phase 2: analysis catalog and version staleness tracking (#285)
        ALTER TABLE analysis_cache ADD COLUMN stale_reason TEXT;

        CREATE TABLE IF NOT EXISTS analysis_catalog (
            plugin_name TEXT NOT NULL,
            co_op_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'proposed',
            proposing_boat TEXT,
            version TEXT,
            author TEXT,
            changelog TEXT,
            proposed_at TEXT NOT NULL,
            resolved_at TEXT,
            reject_reason TEXT,
            data_license_gate_passed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (plugin_name, co_op_id)
        );
        CREATE INDEX IF NOT EXISTS idx_analysis_catalog_co_op
            ON analysis_catalog(co_op_id, state);
    """,
    52: """
        -- Rudder angle table (#419)
        CREATE TABLE IF NOT EXISTS rudder_angles (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL,
            source_addr     INTEGER NOT NULL,
            rudder_angle_deg REAL   NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rudder_angles_ts ON rudder_angles(ts);
    """,
    53: """
        -- Device API keys for headless IoT devices (#423)
        CREATE TABLE IF NOT EXISTS devices (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            key_hash    TEXT NOT NULL,
            role        TEXT NOT NULL DEFAULT 'crew',
            scope       TEXT,
            is_active   INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL,
            last_used   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_devices_key_hash ON devices(key_hash);
    """,
    54: """
        -- ArUco marker tracking (#425)
        CREATE TABLE IF NOT EXISTS aruco_cameras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            ip TEXT NOT NULL,
            marker_size_mm REAL NOT NULL DEFAULT 50.0,
            capture_interval_s INTEGER NOT NULL DEFAULT 60,
            retain_images INTEGER NOT NULL DEFAULT 0,
            calibration JSON,
            calibration_state TEXT NOT NULL DEFAULT 'uncalibrated',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS aruco_camera_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id INTEGER NOT NULL REFERENCES aruco_cameras(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            settings JSON NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            UNIQUE (camera_id, name)
        );

        CREATE TABLE IF NOT EXISTS aruco_controls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            camera_id INTEGER NOT NULL REFERENCES aruco_cameras(id) ON DELETE CASCADE,
            marker_id_a INTEGER NOT NULL,
            marker_id_b INTEGER NOT NULL,
            tolerance_mm REAL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS aruco_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            control_id INTEGER NOT NULL REFERENCES aruco_controls(id) ON DELETE CASCADE,
            distance_cm REAL NOT NULL,
            image_path TEXT,
            session_id INTEGER REFERENCES races(id) ON DELETE SET NULL,
            measured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE INDEX IF NOT EXISTS idx_aruco_measurements_control_time
            ON aruco_measurements(control_id, measured_at DESC);

        CREATE TABLE IF NOT EXISTS aruco_trigger_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phrase TEXT NOT NULL UNIQUE,
            control_id INTEGER NOT NULL REFERENCES aruco_controls(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS aruco_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT OR IGNORE INTO aruco_settings VALUES ('tolerance_mm_default', '5.0');
    """,
    55: """
        -- Unified boat controls (#425)
        CREATE TABLE IF NOT EXISTS controls (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL UNIQUE,
            label         TEXT NOT NULL,
            unit          TEXT NOT NULL DEFAULT '',
            input_type    TEXT NOT NULL DEFAULT 'number',
            category      TEXT NOT NULL DEFAULT 'sail_controls',
            sort_order    INTEGER NOT NULL DEFAULT 0,
            preset_values TEXT,
            created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS control_aruco (
            control_id   INTEGER PRIMARY KEY REFERENCES controls(id) ON DELETE CASCADE,
            camera_id    INTEGER NOT NULL REFERENCES aruco_cameras(id) ON DELETE CASCADE,
            marker_id_a  INTEGER NOT NULL,
            marker_id_b  INTEGER NOT NULL,
            tolerance_mm REAL
        );

        CREATE TABLE IF NOT EXISTS control_trigger_words (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            control_id INTEGER NOT NULL REFERENCES controls(id) ON DELETE CASCADE,
            phrase     TEXT NOT NULL UNIQUE
        );
    """,
    56: """
        -- Configurable control categories (#425)
        CREATE TABLE IF NOT EXISTS control_categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            label       TEXT NOT NULL,
            sort_order  INTEGER NOT NULL DEFAULT 0
        );
    """,
    57: """
        -- Diarized transcript crew association + voice profiles (#443)
        ALTER TABLE transcripts ADD COLUMN speaker_map TEXT;

        CREATE TABLE IF NOT EXISTS crew_voice_profiles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            embedding     BLOB    NOT NULL,
            segment_count INTEGER NOT NULL DEFAULT 0,
            session_count INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT    NOT NULL,
            updated_at    TEXT    NOT NULL,
            UNIQUE(user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_crew_voice_profiles_user ON crew_voice_profiles(user_id);
    """,
    58: """
        -- Session rename + human-readable URL slugs (#449).
        -- slug is NULL until the post-DDL backfill in _migrate_v58_slugs runs.
        ALTER TABLE races ADD COLUMN slug TEXT;
        ALTER TABLE races ADD COLUMN renamed_at TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_races_slug ON races(slug);

        CREATE TABLE IF NOT EXISTS race_slug_history (
            slug       TEXT PRIMARY KEY,
            race_id    INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            retired_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_race_slug_history_race ON race_slug_history(race_id);
    """,
    59: """
        -- Vakaros VKX ingest (#458).  Sessions sourced from a Vakaros Atlas
        -- via the watched-folder ingest path.  Kept in parallel with races(),
        -- linked via matched_race_id when session matching finds an overlap.
        CREATE TABLE IF NOT EXISTS vakaros_sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_hash     TEXT    NOT NULL UNIQUE,
            source_file     TEXT    NOT NULL,
            start_utc       TEXT    NOT NULL,
            end_utc         TEXT    NOT NULL,
            ingested_at     TEXT    NOT NULL,
            matched_race_id INTEGER REFERENCES races(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vakaros_sessions_start ON vakaros_sessions(start_utc);
        CREATE INDEX IF NOT EXISTS idx_vakaros_sessions_match ON vakaros_sessions(matched_race_id);

        CREATE TABLE IF NOT EXISTS vakaros_positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    INTEGER NOT NULL REFERENCES vakaros_sessions(id) ON DELETE CASCADE,
            ts            TEXT    NOT NULL,
            latitude_deg  REAL    NOT NULL,
            longitude_deg REAL    NOT NULL,
            sog_mps       REAL    NOT NULL,
            cog_deg       REAL    NOT NULL,
            altitude_m    REAL
        );
        CREATE INDEX IF NOT EXISTS idx_vakaros_positions_session
            ON vakaros_positions(session_id, ts);

        CREATE TABLE IF NOT EXISTS vakaros_line_positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    INTEGER NOT NULL REFERENCES vakaros_sessions(id) ON DELETE CASCADE,
            ts            TEXT    NOT NULL,
            line_type     TEXT    NOT NULL,  -- 'pin' or 'boat'
            latitude_deg  REAL    NOT NULL,
            longitude_deg REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vakaros_line_session ON vakaros_line_positions(session_id);

        CREATE TABLE IF NOT EXISTS vakaros_race_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id     INTEGER NOT NULL REFERENCES vakaros_sessions(id) ON DELETE CASCADE,
            ts             TEXT    NOT NULL,
            event_type     TEXT    NOT NULL,  -- 'reset'|'start'|'sync'|'race_start'|'race_end'
            timer_value_s  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vakaros_events_session
            ON vakaros_race_events(session_id, ts);

        CREATE TABLE IF NOT EXISTS vakaros_winds (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id     INTEGER NOT NULL REFERENCES vakaros_sessions(id) ON DELETE CASCADE,
            ts             TEXT    NOT NULL,
            direction_deg  REAL    NOT NULL,
            speed_mps      REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vakaros_winds_session ON vakaros_winds(session_id, ts);
    """,
    60: """
        ALTER TABLE races ADD COLUMN vakaros_session_id INTEGER
            REFERENCES vakaros_sessions(id) ON DELETE SET NULL;
        CREATE INDEX IF NOT EXISTS idx_races_vakaros_session
            ON races(vakaros_session_id);
    """,
}

def _split_migration_sql(sql: str) -> list[str]:
    """Split a migration string into individual SQL statements."""
    stmts: list[str] = []
    for raw in sql.split(";"):
        lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            idx = stripped.find("--")
            if idx >= 0:
                stripped = stripped[:idx].rstrip()
            if stripped:
                lines.append(stripped)
        stmt = " ".join(lines).strip()
        if stmt:
            stmts.append(stmt)
    return stmts

async def apply_migrations(engine: DatabaseEngine) -> None:
    """Apply any pending schema migrations."""
    db = engine.write_conn()

    await db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    await db.commit()

    cur = await db.execute("SELECT MAX(version) FROM schema_version")
    row = await cur.fetchone()
    current = row[0] if row and row[0] is not None else 0

    repaired = 0
    for version in sorted(_MIGRATIONS):
        if version > current:
            break
        for stmt in _split_migration_sql(_MIGRATIONS[version]):
            upper = stmt.lstrip().upper()
            is_create = upper.startswith("CREATE TABLE IF NOT EXISTS") or upper.startswith(
                "CREATE INDEX IF NOT EXISTS"
            )
            is_alter_add = upper.startswith("ALTER TABLE") and "ADD COLUMN" in upper
            if not (is_create or is_alter_add):
                continue
            if "_new " in stmt or "_new(" in stmt:
                continue
            try:
                await db.execute(stmt)
                repaired += 1
            except Exception:
                pass
    if repaired:
        await db.commit()

    for version in sorted(_MIGRATIONS):
        if version <= current:
            continue
        logger.info("Applying schema migration v{}", version)
        for stmt in _split_migration_sql(_MIGRATIONS[version]):
            upper = stmt.lstrip().upper()
            is_alter_add = upper.startswith("ALTER TABLE") and "ADD COLUMN" in upper
            if is_alter_add:
                try:
                    await db.execute(stmt)
                except Exception:
                    pass
            else:
                await db.execute(stmt)
        await db.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        await db.commit()

    logger.debug("Schema is at version {}", _CURRENT_VERSION)
