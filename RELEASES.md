# Release Notes

## Sprint 5 — Web Architecture, Networking & Bandwidth, Calibration (2026-03-24)

Major web architecture refactor, metered bandwidth management, admin networking
tools, and continued analysis and visualization work.

### Web architecture overhaul
- **Route module split** (#405) — decomposed monolithic `web.py` into 24
  domain-specific route modules for maintainability
- **WebSocket live push** (#405) — real-time instrument data streaming to the
  browser via WebSocket
- **WAL mode + connection split** (#405) — SQLite write-ahead logging with
  separate read/write connections for improved concurrency

### Metered bandwidth management (#403)
- **Metered-connection mode** — detects cellular/marina networks and enforces
  bandwidth budgets with configurable daily limits
- **InfluxDB bandwidth attribution** — per-path network tagging so dashboard
  panels show exactly where bandwidth is spent
- **Grafana dashboard** — pre-built panels for bandwidth monitoring and alerting

### Admin networking & remote access
- **Network management page** (#256) — WLAN profile switching, interface status,
  and bandwidth monitoring from the admin UI
- **Tailscale, Cloudflare & DNS** (#256) — Tailscale status, Cloudflare routing,
  and DNS configuration on the network page
- **Cloudflare Tunnel wizard** (#378) — interactive setup for remote access
  without port forwarding
- **Config persistence** — InfluxDB and camera config saved to
  `~/.helmlog/config.env` to survive deploys
- **User deletion** (#381) — Delete button on the admin users page

### Calibration & boat settings
- **Instrument calibration category** (#337) — new `instrument_calibration`
  settings category for compass offset, speed factor, etc.
- **Negative value input** (#407) — ± toggle button for mobile numeric inputs
  (e.g., compass deviation)
- **Session settings fix** (#385) — boat setup values now correctly reflected
  during and after active sessions

### Analysis & visualization
- **Analysis framework Phase 2** (#285) — co-op plugin promotion, A/B
  comparison workflow, and version staleness tracking
- **Analysis catalog UI** (#412) — admin page for plugin management, co-op
  catalog with state badges and moderator actions, session-page staleness
  indicator with re-run button, and A/B comparison side-by-side view
- **Visualization framework Phase 1** (#286) — pluggable chart rendering
  for session data with extensible plugin API
- **Session deletion** (#409) — delete sessions from the UI with an
  active-session safety guard

### Auth & profile
- **Change password** (#340) — `PATCH /api/me/password` endpoint with full
  validation chain (credential check, current password verification, length,
  confirmation match) and audit logging. Profile page conditionally renders
  the form only for password-credential users

### Signal K fixes
- **Self-vessel UUID resolution** (#388) — Signal K deltas no longer rejected
  due to mismatched vessel identity
- **Admin API access** (#384) — use `type=admin` in Signal K security.json for
  proper admin-level access

### Theming & UX
- **CSS variable migration** — remaining hardcoded hex colors replaced with CSS
  custom properties across all templates, JS, and Python
- **In-app issue reporting** (#369) — bug report and feature request links
  directly from the app

### Documentation & developer experience
- **Crew race-day guide** — quick reference for crew operating HelmLog during
  races (start/stop sessions, mark events, troubleshooting)
- **AI contributor guardrails** (#392) — improved onboarding docs and CI checks
  for AI-assisted contributions
- **Semantic layer** (#390) — machine-readable domain knowledge module with
  decision tables, state diagrams, and EARS specs
- **Documentation refresh** (#172, #236) — roadmap, README, CONTRIBUTING.md,
  and guides updated to match current codebase

### Infrastructure
- **CI dependency bumps** — actions/checkout v4→v6, actions/github-script v7→v8
- **WAL gitignore** — SQLite WAL files excluded to prevent dirty-version false
  positives on Pi deploys
- **Idempotent migrations** — `ALTER TABLE ADD COLUMN` made safe to re-run

## Sprint 4 — Analysis, Session Matching, Tuning Extraction & Skill Tooling (2026-03-18)

Sprint 4 adds a pluggable analysis framework, co-op session matching,
transcript-based tuning extraction, customizable color schemes, and
expanded developer skill tooling.

### Pluggable analysis framework (#283, #309, #321)

Extensible plugin system for post-session analysis:

- **Plugin protocol** — ABC-based plugin API with importlib discovery, SQLite
  cache with data-hash invalidation (schema v42), and preference inheritance
  (platform → co-op → boat → user)
- **Polar baseline plugin** — wraps existing polar analysis as the first
  built-in plugin
- **Sail VMG comparison** (#309) — pure upwind/downwind VMG functions across 5
  wind bands, cross-session `/api/sails/performance` endpoint, and performance
  table on the sails page

### Co-op session matching (#281, #324)

Proximity-based pairing of co-op sessions across boats:

- **Automatic scan** — configurable time window (15 min) and geographic radius
  (2 NM) to detect overlapping sessions from co-op peers
- **Match lifecycle** — Unmatched → Candidate → Matched → Named, with quorum
  confirmation and shared name synthesis
- **Federation support** — 5 new peer API endpoints for scan, proposal, confirm,
  reject, and name push — all Ed25519-signed
- **Scalability** — parallel fan-out for 20-boat co-ops, proposal dedup,
  centroid caching (schema v45)

### Boat tuning extraction from transcripts (#276, #325)

Regex-based extraction of tuning parameters from audio transcripts:

- **RegexExtractor** — parses natural language ("backstay 12", "vang 8.5") from
  transcript segments into structured tuning values
- **Extraction run lifecycle** — Created → Running → ReviewPending/Empty →
  FullyReviewed, with accept/dismiss review workflow
- **Auto-create settings** — accepted items automatically create `boat_settings`
  timeline entries linked to the extraction run
- **Review UI** — collapsible settings history with transcript play buttons,
  newest-first timeline, and superseded-default indicators
- **Privacy** — all extraction data is boat-private, never shared with co-op
- Schema v44 adds `extraction_runs` and `extraction_items` tables; 7 API endpoints

### Customizable color schemes (#347, #358)

Sunlight-optimized theming system:

- **6 presets** with WCAG contrast validation
- **Admin default + user override** — boat-wide default on admin settings,
  personal override on profile page
- **Full compliance** — ~150+ hardcoded hex colors replaced with CSS custom
  properties across all templates, JS, CSS, and Python

### Threaded comments Phase 2 — notifications (#284, #321)

- **@mention autocomplete** — dropdown in comment textareas with arrow key
  navigation, multi-word name support
- **4 notification types** — mention, new thread, reply, resolved — with
  pluggable channels (platform + email)
- **Attention dashboard** — `/attention` page with nav badge for unread
  notifications
- Schema v43 for notification storage

### Sail management overhaul (#306, #307, #308, #318)

- **Point-of-sail field** — upwind/downwind/both classification per sail
  (schema v39)
- **Default sail selection** — `sail_defaults` table (schema v40) with
  pre-selection on session pages
- **Sail management page** — `/sails` with inventory, accumulated tack/gybe
  counts, per-session history with wind summaries

### Auto-start recording (#345, #346)

- Schedule a future race start time via the home page UI
- Background task fires `start_race()` at the scheduled time with 1 s polling
- Manual start atomically cancels any pending schedule; missed starts are
  detected and cleared

### Developer experience & infrastructure

- **Risk tiers** (#320) — 4-tier classification (Critical/High/Standard/Low)
  with tier-aware `/pr-checklist` and `/spec` skill for structured specs
- **Skill evaluation framework** (#349, #357) — test cases for measuring skill
  quality and detecting regressions
- **/skill-compare** (#354, #367) — blind A/B comparison of two skill versions
  with correctness, completeness, conciseness, and actionability scoring
- **Skill trigger optimization** (#353, #364) — audited descriptions for all 13
  skills with explicit trigger/anti-trigger guidance and 33-case test suite
- **/architecture skill** (#352, #363) — codebase comprehension with module map,
  data flow, and complexity hotspots
- **/diagnose skill** (#351, #360) — systematic Pi troubleshooting runbook
- **/domain skill** (#350, #359) — Signal K paths, NMEA 2000 PGNs, and sailing
  instrument reference
- **Pi test harness** (#334, #335, #336) — Mac-orchestrated cross-Pi federation
  testing over Tailscale with UI smoke tests
- **Claude Code Review** (#330) — GitHub Actions workflow for automated PR review
- **CI updates** — actions/checkout v6, astral-sh/setup-uv v7
- **Pi fixes** — sudoers exact-match fix (#361), data dir ownership (#362),
  Grafana provisioning permissions (#361)

---

## Sprint 3 — Auth, Boat Settings, Comments & Developer Tooling (2026-03-14)

Sprint 3 adds multi-method authentication, boat tuning capture, and
collaborative race discussion.

### Authentication overhaul — invitation + password + OAuth (#268, #272, #279, #280)

Replace the single-use magic-link flow with a proper auth system:

- **Invitation workflow** — admins send email invitations; users register with
  a password on first visit
- **Password authentication** — bcrypt-hashed credentials with forgot/reset flow
- **OAuth login** — Google, Apple, and GitHub identity providers via Authlib
- **Session middleware** — secure cookie-based sessions replace per-request
  token lookup
- **Developer role** (#271) — orthogonal `is_developer` flag gates access to
  the synthesizer and non-standard branch selection in the deploy UI

### Boat tuning settings (#274, #275, #297, #298)

Structured capture and playback of boat tuning parameters:

- **Time-series data model** — `boat_settings` table (schema v35) stores
  parameter values with timestamps, supporting both boat-level defaults and
  race-specific overrides
- **Manual input UI** — phone-friendly accordion card on the home page with
  category groups ordered by change frequency, auto-save on field change
- **Session playback panel** — read-only settings panel on the session detail
  page resolves values by timestamp; overridden defaults shown with
  strikethrough annotation; updates when clicking the track or scrubbing video
- **Synthesizer integration** — synthesized sessions now generate realistic
  J/105 tuning data (rig tensions, sail controls, wind-responsive adjustments)

### Threaded comments Phase 1 (#304)

Collaborative race discussion anchored to sessions:

- **Comment threads** — create threads on a session with optional timestamp or
  mark reference (weather_mark_1, leeward_mark_2, etc.)
- **Threaded replies** — nested comments with per-user read/unread tracking and
  unread badges
- **Resolve/unresolve** — mark threads as resolved with a summary; resolved
  threads show as hollow green rings on the track map
- **Track map integration** — right-click the track to start a discussion at
  that timestamp; colored dots show open (purple), unread (blue), and resolved
  (green) threads with tooltip previews
- **Crew redaction** — comment content is redacted in co-op API responses per
  data licensing policy

### Developer experience & infrastructure

- **Promote gate** (#302, #303) — `promote.yml` GitHub Actions workflow gates
  `main → stage` promotion on a new RELEASES.md heading; ideation-only commits
  are exempt
- **/release-notes skill** — drafts a RELEASES.md entry from commits since
  the last stage promotion
- **/ideate skill** (#289) — capture half-baked ideas into `docs/ideation-log.md`
  with structured metadata
- **Bootstrap fixes** (#291, #292, #294) — admin login URL printed correctly,
  `sudo` added to data dir removal in `reset-pi.sh`

---

## Sprint 2 complete — Performance Analysis & Synthesizer (2026-03-12)

Sprint 2 (March 10–24) is feature-complete. 

### Performance analysis

- **Maneuver detection** (#232) — automatic tack and gybe detection from 1 Hz
  heading data, surfaced on the session detail page
- **Polar performance visualization** (#233) — polar diagram overlay on the
  session detail page showing boat performance against the J/105 target polar

### Synthesizer improvements

- **Spatially varying wind model** (#248) — wind direction and pressure
  gradients across the course area instead of a single uniform wind field
- **Tack on headers with realistic randomization** (#247) — synthesized boats
  now tack when lifted, with heading noise and timing jitter for realistic
  tracks
- **Wind model sharing between co-op members** (#246) — co-op boats in a
  synthesized session share the same wind field and start time so their tracks
  are physically consistent
- **Fix: leg-derived marks for wind field visualization** (#264) — wind field
  overlay now uses the actual mark positions from leg geometry instead of the
  original placed marks

### Deploy & infrastructure

- **Promotion history and branch comparison** (#258) — deploy admin page shows
  a commit-level diff between the current branch and the promotion target
- **Fix: create loki/promtail groups before chown** (#261) — setup.sh no longer
  fails on a fresh Pi when the loki/promtail system groups don't exist yet

---

## Promoted to live and stage from main, 2026-03-10

### Synthesize race sessions with interactive Leaflet map (#245, #252)

Generate synthetic J/105 sailing sessions for testing and demo purposes:

- **Interactive course builder** — Leaflet map with CYC mark pins; click to
  place the race committee boat, auto-compute windward/leeward marks
- **Simulation engine** — J/105 polar interpolation, wind shifts, VMG-aware
  tack selection, and 1 Hz instrument data generation
- **Land avoidance** — real OSM coastline data for Puget Sound; computed marks
  that fall on land are automatically pulled to navigable water
- **Segment-based land crossing detection** — intermediate point sampling
  catches peninsula/island clips even when both endpoints are in water
- Synthesized session type with amber badge in UI, filterable on history page

### Federation phase 1 — identity, co-op data model, session sharing (#224)

Boat-to-boat federation with cryptographic identity:

- **Ed25519 keypair generation** — `helmlog identity init` creates a boat card
  with public key and fingerprint
- **Co-op data model** — create, join, and manage cooperative groups of boats
- **Session sharing** — share/unshare sessions with co-op members, with embargo
  enforcement and audit logging
- **Peer API** — inter-boat HTTP endpoints with request signing and verification
- 32 integration tests covering co-op lifecycle, auth, embargo, and data licensing

### Deployment management and promotion workflow (#222, #223)

Structured release process with stage/live branches:

- **Admin deploy page** — view current version, trigger updates, and monitor
  deploy status from the web UI
- **Evergreen mode** — opt-in automatic deployment on push to the tracked branch
- **Promotion workflow** — `main` → `stage` → `live` branch promotion with
  safety checks

### Data policy compliance — privacy, auth, and deletion controls (#194–#211, #215)

Comprehensive data licensing policy enforcement:

- **PII deletion** — audio, photos, transcripts, and diarized content can be
  deleted per data policy requirements
- **Field allowlists** — co-op API endpoints enforce strict field filtering
- **Audit logging** — all data access and sharing events are logged
- **Private session isolation** — unshared sessions are invisible to co-op peers

### Configurable Pi health monitor interval (#249, #250)

- Default collection interval changed from 60 s to 2 s for smoother dashboards
- Non-blocking CPU measurement via `psutil.cpu_percent(interval=None)`
- `MONITOR_INTERVAL_S` env var and admin settings page for runtime tuning

### Mobile navigation overhaul (#230, #231)

- **Hamburger menu** replaces tab navigation for better mobile usability

### Infrastructure and developer experience

- **GitHub Actions CI** — tests, lint, and type checking on every PR (#219)
- **Docker dev container** — Claude Code development environment (#229)
- **Community contribution infrastructure** — CONTRIBUTING.md, issue templates,
  PR template (#174, #218)
- **Fan speed** added to Pi Health Grafana dashboard with shared crosshairs (#225)
- **Hostname in footer** — version info now includes the Pi hostname (#175, #221)
- **reset-pi.sh** — restore a Pi to pre-setup state for reimaging (#242)
- **Git ownership fix** — prevent `.git/` conflicts between helmlog service and
  deploy user (#239, #240)
- **Bootstrap add-user fix** — run as correct user to avoid read-only DB (#244)
- **Documentation** — approachability guides, fleet quickstart, operator updates,
  federation protocol gaps (#216, #217, #220)
- **CLAUDE.md** — added `uv sync` dependency guidance (#251)

---

## 2026-03-08

### Embedded YouTube player with track sync (#183, #185)

Session replay now includes synchronized video:

- **Embedded YouTube player** on session detail page and history page cards
- **Bidirectional track sync** — click a point on the track map and the video
  jumps to that moment; video playback updates the track marker position
- Deep-link support via `?t=<seconds>` for sharing specific race moments

### Session detail page with track map (#178, #180)

Dedicated session view at `/session/{id}`:

- **Interactive track map** — GPS track rendered with speed-based color coding
- **Video deep-links** — clickable timestamps jump to the corresponding video
- All session metadata (crew, results, notes, transcripts, sails, exports)
  consolidated into a single page replacing the history accordion cards

### Simplified home page (#170, #171)

Home page redesigned for race-day focus:

- **Idle state** — start buttons only (race, practice, debrief)
- **Active state** — current race card with live instruments and controls
- Red stop button with two-tap safety guard and countdown timer
- Camera start/stop runs fire-and-forget so race API responds instantly
- Extracted inline HTML/CSS/JS from `web.py` into Jinja2 templates and
  static files (`base.html`, `home.html`, `history.html`, `base.css`,
  `shared.js`, `home.js`, `history.js`)

### Gaia GPS backfill (#101, #176)

Import historical race data from Gaia GPS exports:

- **Track download** — fetch GPX tracks from Gaia GPS API
- **Race classification** — auto-detect race sessions from track patterns
- **SQLite import** — backfill position, heading, and speed data
- **InfluxDB migration** — push historical data to Grafana dashboards

### Insta360 X4 video pipeline (#155, #161)

End-to-end automated video workflow:

- **Camera control** — start/stop recording via OSC HTTP API, tied to race
  start/stop events (#98, #147)
- **Camera admin UI** — add/configure cameras, WiFi credentials, status
  monitoring from the web interface (#147)
- **Stitch & upload** — Docker-based stitcher with ffmpeg fallback; discovers
  both `.insv` (360°) and `.mp4` (single-lens) recordings
- **Auto-link** — uploaded videos automatically associated with race sessions
  by time overlap
- WiFi SSID/password fields on camera config for direct camera network access

### Remote transcription offload (#121, #146)

Offload Whisper transcription from the Pi to a faster machine:

- **HTTP worker** (`scripts/transcribe_worker.py`) runs on a Mac or other
  machine with more CPU
- **Admin settings page** — SQLite-backed settings overrides configurable
  from `/admin/settings` in the web UI
- Transparent fallback to local Pi transcription if remote worker is
  unreachable

### Nginx reverse proxy (#137, #167)

Single-port access to all services on the Pi:

- **Path-based routing** — `/` (Helm Log), `/grafana/` (Grafana), `/sk/`
  (Signal K admin), `/signalk/` (Signal K API)
- Eliminates the need to remember multiple port numbers
- Signal K admin UI moved to `/sk/` to avoid conflict with `/admin/`

### Configurable event naming rules (#154, #159)

Day-of-week event names are now admin-configurable:

- Admin UI at `/admin/events` for creating and managing event rules
- Custom event names (set via web UI) take precedence over weekday defaults
- Error message shown when starting a race without a daily event set (#153)

### Self-service login (#148, #152)

Existing users can request a new magic link from the login page without
needing an admin to generate an invite token.

### Configurable Pi host references (#151)

Removed hardcoded `corvopi` / `weaties` references from scripts and docs.
All Pi-specific values now come from environment variables or are
auto-detected.

### Infrastructure fixes

- **Loki + Promtail** for centralized log management (#139, #142), with
  loopback-only config fix (#162)
- **Setup.sh fixes** — bcrypt module, data/ permissions, uv Python
  traversal, tmux, locale (#140, #141, #144)
- **Signal K** — fix CORS origins crash on fresh install (#143)
- **Grafana** — bind to 0.0.0.0 for Tailscale access, auto-populate
  InfluxDB vars in `.env`, anonymous access with None role for login page
- **Race Track query optimization** — downsample before union, 65x faster
- **Database schema documentation** at `docs/database-schema.md` (#156, #157)

### Shared navigation, version footer, timezone support (#129, #130)

Consistent navigation and timezone-aware timestamps across all pages:

- **Shared nav bar** — Home, History, Boats, Users (admin), Audit (admin), and
  Profile links appear on every page; admin-only links auto-hide for non-admins
- **Version footer** — every page shows the git branch, short SHA, and
  dirty/clean status (includes uncommitted changes and unpushed commits)
- **Configurable timezone** — set `TIMEZONE=America/Los_Angeles` (or any IANA
  name) in `.env` to display all timestamps in local time instead of UTC; affects
  race date grouping, weekday event naming, and all displayed timestamps on home,
  history, audit, and admin pages
- 12 new tests (425 total passing)

### Audit log, tags, triggers, headshots, admin nav (#93, #94, #99, #100, #123)

Major feature batch adding traceability, organization, and user personalization:

- **Audit logging** — all state-changing web routes log user, IP, and action to
  an `audit_log` table; admin audit page at `/admin/audit` + JSON API at `/api/audit`
- **Tags** — full CRUD API for tagging sessions and notes; `tags`, `session_tags`,
  `note_tags` tables (schema v19)
- **Keyword triggers** — auto-create notes from transcript keywords; new
  `triggers.py` module + `scan-transcript` CLI subcommand for retroactive scanning
- **Profile headshots** — self-service avatar upload on profile page (Pillow
  resize to 256×256 JPEG); SVG initials fallback for users without avatars
- **Admin Users nav link** — `/admin/users` is now discoverable from the main
  nav bar (visible to admins only via `/api/me` role check)
- 25 new tests (413 total passing)

### Speaker diarisation on Pi 5 (#120)

Pyannote speaker diarisation now works on the Pi 5 (aarch64):

- Enabled `pyannote-audio` pipeline for speaker labeling (`SPEAKER_00`,
  `SPEAKER_01`, …) on transcriptions when `HF_TOKEN` is configured
- Bypassed `torchcodec` `AudioDecoder` which fails on aarch64 — uses
  `soundfile` fallback instead
- Set `HOME` env var in systemd unit so `faster-whisper` can cache models
  under the `helmlog` service account

### Security hardening (#117, #118)

Comprehensive security hardening of the Pi deployment, baked into `setup.sh`
so all future SD card builds inherit the same posture:

- Dedicated `helmlog` service account (nologin, UID ≈ 997)
- Scoped `NOPASSWD` sudo replacing the blanket Pi OS default
- SSH hardening (X11Forwarding disabled, permissions tightened)
- InfluxDB bound to loopback only; Grafana loopback + login required
- Automatic security updates via `unattended-upgrades`
- Unused services masked (cups, avahi-daemon, bluetooth)
- Signal K bcrypt admin password auto-generated at setup
- SSH safety guard added after lockout incident — `setup.sh` now validates
  that at least one SSH access method remains before tightening config

### Race event naming fix (#122)

Custom event names (saved via the web UI) now take precedence over the
weekday defaults (BallardCup on Monday, CYC on Wednesday). Previously,
a saved custom event was ignored on Monday/Wednesday.

### Security audit documentation

Added security audit report and penetration test statement of work
(`docs/`) documenting the 2026-03-01 security review.

---

## 2026-03-01

### Audio transcription (#42, PR #63)

Completed recordings can now be transcribed to text directly from the History
page. Transcription runs on the Pi via
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) — no cloud service
required.

- **📝 Transcript** button on every audio-enabled race card in History
- Jobs run in the background; status polling shows a spinner until complete
- Transcripts are stored in SQLite (`transcripts` table, schema v15)
- Model is configurable via `WHISPER_MODEL` env var (default: `base`)
- Speaker diarisation deferred — pyannote.audio is too heavy for Pi CPU

### System health monitoring (#39, PR #62)

The logger now watches the Pi's vitals automatically.

- `monitor_loop` background task collects CPU/memory/disk/temperature via
  `psutil` every 60 s and writes a `system_health` measurement to InfluxDB
- Home page polls `/api/system-health` every 30 s; a red warning banner
  appears when disk > 85 % or CPU temp > 75 °C
- `GET /api/system-health` endpoint available for external monitoring

### Audio playback and download (#21, PR #61)

WAV recordings can now be played or downloaded directly from the web UI
without needing to SSH into the Pi.

- Inline `<audio>` player on every History race card with an associated recording
- `GET /api/audio/{id}/stream` — browser-range-request compatible streaming
- `GET /api/audio/{id}/download` — downloads the WAV with `Content-Disposition: attachment`

### Photo caching (#44, PR #61)

Photo notes no longer reload on every page refresh, which was noticeably slow
over the boat's Wi-Fi hotspot.

- `serve_note_photo` now returns `ETag` + `Cache-Control: public, max-age=31536000, immutable`
- `304 Not Modified` responses on repeat loads (effectively free after first load)
- `loading="lazy"` on all photo `<img>` tags

---

## 2026-02-27

### Sail tracking (#57, PR #60)

- Sail inventory management on the Boats page (add / delete sails by type and name)
- Per-race sail selection on History race cards (main, jib, kite)
- Sail choices stored in `race_sails` table (schema v14)

### Guest crew position, WAV player on home page, debrief audio (#38, #49, #31, PR #59)

- Crew member positions include a **Guest** option
- The home page shows a live audio player when a recording is in progress
- Debrief sessions can be added with a named crew list

### YouTube video linking with Grafana deep links (#22, PR #51 / #58)

- Link any YouTube video to instrument data via a UTC/offset sync point
- Every CSV export row gets a `video_url` deep-link (`?t=<seconds>`)
- Grafana annotation popups link directly to the matching video timestamp
- History page **Add Video** form with optional sync calibration

### Grafana annotations endpoint (#52, PR #54)

- `POST /api/grafana/annotations` — creates Grafana annotations from race/practice events
- Enables click-through from Grafana time-series panels to race timestamps

### Git version in web UI footer (#55, PR #56)

- Branch name and short commit SHA shown in the footer of every web page
- Makes it easy to confirm which version is running on the Pi

### Grafana InfluxDB datasource and dashboards (#earlier)

- Boatspeed, wind, heading, depth, position — all provisioned at setup time
- `can-interface.service` → `signalk.service` → `helmlog.service` dependency chain

### External data: weather and tides

- Open-Meteo hourly weather (wind, air temp, pressure) fetched once per hour
- NOAA CO-OPS hourly tide predictions fetched once per day
- Both written to SQLite and included as extra columns in CSV exports

### Audio recording

- Automatic WAV recording from USB Audio Class devices (Gordik 2T1R tested)
- One file per session in `data/audio/`, named by UTC start timestamp
- Graceful degradation — no device means instrument logging continues unaffected
