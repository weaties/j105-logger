## Summary

AIS (Automatic Identification System) data is a public VHF radio broadcast (161.975 / 162.025 MHz). Every vessel with an AIS transceiver — required for most racing fleets — transmits position, SOG, COG, and MMSI on a continuous cycle. This data is already captured and sold commercially by MarineTraffic, VesselFinder, and dozens of other services. There is no legal expectation of privacy for AIS transmissions.

HelmLog currently blocks all AIS data (issue #208) based on the principle that the co-op should not become a surveillance tool. That principle was sound for *instrument data* (polars, wind angles, heel, trim) which is genuinely private. But it conflates two fundamentally different categories:

1. **Instrument data** — private, requires consent, co-op sharing model is correct
2. **AIS broadcast data** — public radio transmission, no consent framework exists or is needed

By refusing to capture AIS data, we are voluntarily blinding ourselves to publicly available information that would unlock significant performance analysis capabilities — without requiring any other boat to install helmlog or join a co-op.

## What this unlocks

- **Fleet-relative performance** — how we performed against every AIS-equipped boat, not just co-op members
- **Start-line analysis** — position in the line relative to the fleet, VMG off the line, gap analysis
- **Mark rounding replay** — distance gained/lost at each mark vs. specific competitors
- **Leg-by-leg delta tracking** — gained 200m upwind, gave back 150m on the run
- **Lane analysis** — which side of the course paid, who went there, what happened
- **Fleet statistics** — spread over time, compression/expansion, fleet convergence at marks
- **Post-race debrief overlay** — replay our track alongside the fleet without needing anyone else's cooperation

## Scope of changes

### 1. Data licensing policy — docs/data-licensing.md

- **Rewrite Section 1.1** (AIS and proximity data exclusion) to distinguish between:
  - AIS broadcast data (public, captured freely)
  - Instrument/proximity data from non-members (still excluded — radar targets, DSC, etc.)
- **Update Section 7** (Non-Member Boats) to allow AIS position capture while maintaining the prohibition on instrument-level surveillance
- **Add new section** on AIS data retention, usage scope, and deletion policy
- **Update plain-English summary** to reflect the new distinction
- **Update technical requirements table** to replace blanket AIS exclusion with scoped rules

### 2. NMEA 2000 PGN handling — src/helmlog/nmea2000.py

- **Split AIS_BLOCKED_PGNS** into two sets:
  - AIS_POSITION_PGNS — PGNs to capture (Class A/B position reports: 129038, 129039, 129040; static data: 129794, 129809, 129810)
  - AIS_BLOCKED_PGNS — PGNs that remain blocked (safety messages, channel management, interrogation, etc.)
- **Add AIS dataclasses**: AISPositionReport, AISStaticData with fields for MMSI, lat, lon, SOG, COG, heading, nav status, vessel name, callsign, ship type, dimensions
- **Add PGN decode functions** for the captured AIS PGNs

### 3. Signal K reader — src/helmlog/sk_reader.py

- **Remove the blanket AIS path rejection** (the `if "ais" in path.lower()` check)
- **Add AIS vessel subscription**: subscribe to vessels.* context for AIS-relevant paths only:
  - navigation.position, navigation.speedOverGround, navigation.courseOverGroundTrue
  - navigation.headingTrue, design.length, design.beam
  - name, mmsi, communication.callsignVhf
- **Add geographic filter**: only capture AIS targets within a configurable radius of own position (default: 5 nm) to scope to the sailing area, not every commercial vessel transiting nearby
- **Emit AISPositionReport records** through the same callback pipeline as own-boat records

### 4. CAN reader — src/helmlog/can_reader.py

- **Remove AIS PGNs from the blocklist** (the position/static ones only)
- **Add decode path** for AIS position PGNs to AISPositionReport records
- **Apply same geographic filter** as SK reader

### 5. Storage — src/helmlog/storage.py

New schema migration (v51) adding:

```sql
-- Vessel registry: one row per unique MMSI seen
CREATE TABLE ais_vessels (
    mmsi TEXT PRIMARY KEY,
    name TEXT,
    callsign TEXT,
    ship_type INTEGER,
    length_m REAL,
    beam_m REAL,
    first_seen TEXT NOT NULL,      -- UTC ISO 8601
    last_seen TEXT NOT NULL,       -- UTC ISO 8601
    is_race_fleet INTEGER DEFAULT 0  -- manual tag for known competitors
);

-- AIS position log: high-frequency during sessions, sampled otherwise
CREATE TABLE ais_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,              -- UTC ISO 8601
    mmsi TEXT NOT NULL REFERENCES ais_vessels(mmsi),
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    sog_kts REAL,
    cog_deg REAL,
    heading_deg REAL,
    nav_status INTEGER,
    session_id INTEGER REFERENCES races(id),  -- NULL if not during a session
    FOREIGN KEY (mmsi) REFERENCES ais_vessels(mmsi)
);

CREATE INDEX idx_ais_positions_ts ON ais_positions(ts);
CREATE INDEX idx_ais_positions_mmsi_ts ON ais_positions(mmsi, ts);
CREATE INDEX idx_ais_positions_session ON ais_positions(session_id);
```

Storage methods needed:
- store_ais_position(report: AISPositionReport, session_id: int | None)
- store_ais_vessel(static: AISStaticData)
- get_ais_positions(start, end, mmsi, session_id)
- get_ais_vessels(race_fleet_only: bool = False)
- tag_race_fleet(mmsi: str, is_race_fleet: bool)
- AIS data pruning: configurable retention (default 90 days for non-session data; session-linked data follows session retention)

### 6. Configuration — .env.example, main.py

New environment variables:
- AIS_ENABLED=true — master toggle (default: false for backward compat on first release)
- AIS_RADIUS_NM=5 — geographic filter radius in nautical miles
- AIS_RETENTION_DAYS=90 — retention for non-session AIS positions
- AIS_SAMPLE_INTERVAL_S=5 — minimum interval between stored positions per vessel (avoid flooding storage with Class A 2-second reports)

### 7. Export — src/helmlog/export.py

- **Fleet GPX export**: session export gains optional include_fleet=True flag — adds AIS tracks as additional trk elements in GPX, each named with vessel name/MMSI
- **Fleet CSV export**: separate CSV with columns ts, mmsi, name, lat, lon, sog, cog
- **Fleet JSON export**: AIS tracks alongside own-boat data for Sailmon/regatta tool import
- Own-boat exports remain unchanged when include_fleet=False (default)

### 8. Web interface

- **Session detail page** (templates/session.html, static/session.js):
  - Fleet replay overlay on the map — toggle AIS tracks on/off
  - Competitor selector — filter which vessels to show
  - Delta chart — distance to selected competitor(s) over time
- **Fleet management page** (new admin template):
  - List of known AIS vessels with name, MMSI, last seen
  - Tag vessels as race fleet for filtering
  - AIS data statistics (storage usage, record counts)
- **History page**: fleet count badge on sessions that have AIS data

### 9. Race analysis — new module src/helmlog/fleet_analysis.py

- compute_fleet_deltas(session_id, target_mmsi) — distance/time delta to a competitor over the session
- compute_start_analysis(session_id) — line position, time-to-line, distance-to-line for fleet
- compute_mark_rounding_deltas(session_id) — gaps at each mark (requires marks to be set)
- compute_lane_analysis(session_id) — lateral separation from fleet center-of-mass per leg

### 10. Skill updates

| Skill | Change |
|---|---|
| /domain | Add AIS PGN reference, SK vessel paths, AIS message types and update rates |
| /data-license | Update checklist: AIS broadcast data is permitted; instrument-level surveillance still excluded |
| /architecture | Update module map to include fleet_analysis.py and AIS data flow |

### 11. Documentation

- **CLAUDE.md**: Update architecture principles to note AIS as a second data category; update module list with fleet_analysis.py
- **walkthrough.md**: Rewrite AIS blocking section to explain the new dual policy (AIS=captured, instrument surveillance=blocked)
- **docs/data-licensing.md**: As described in item 1
- **README.md**: Add AIS fleet tracking to feature list

### 12. Federation / co-op interaction

- **AIS data is NOT shared via co-op** — it is local-only. Every boat captures its own AIS feed. No federation changes needed for AIS position data
- **Co-op remains for instrument data** — polars, wind, heel, trim still require voluntary sharing
- **Optional future**: co-op members could cross-reference their AIS captures to build a richer fleet picture, but that is a separate feature and not in scope here
- peer_api.py, peer_client.py, peer_auth.py — **no changes** in this issue

### 13. Tests

- Unit tests for AIS PGN decoding (new dataclasses, decode functions)
- Unit tests for SK reader AIS path acceptance and geographic filtering
- Unit tests for storage: AIS position/vessel CRUD, retention pruning, session linking
- Unit tests for fleet analysis computations
- Unit tests for export with fleet data included
- Update existing tests that assert AIS rejection to reflect new selective policy
- No integration test changes (AIS is local, not federated)

## Migration path

1. **Default off**: AIS_ENABLED=false — existing deployments see zero behavior change
2. **Opt-in**: user sets AIS_ENABLED=true in .env and restarts
3. **Schema migration**: v51 runs automatically, adds tables (no impact if AIS is disabled)
4. **Future default**: after validation, flip default to true in a subsequent release

## What this does NOT change

- Co-op trust model — instrument data sharing is unchanged
- Privacy for instrument data — polars, wind angles, heel, trim still require co-op membership
- Radar/DSC blocking — only AIS broadcast position data is captured; radar targets and DSC remain blocked
- Protest firewall — AIS data is not formatted for protest committee use
- Gambling prohibition — unchanged

## Open questions

- [ ] Should we support AIS-B (Class B) extended position reports differently from Class A? (Class B has lower update rate — may not need sampling)
- [ ] Do we want a regatta mode that increases sampling rate during active sessions vs. background capture?
- [ ] Should the geographic filter be a hard circle or a bounding box? (Circle is more intuitive but box is cheaper to compute)
- [ ] MMSI-to-boat-name resolution: rely solely on AIS static data messages, or allow manual entry in the fleet management UI?

---

*This reverses the blanket AIS prohibition from #208. The core principle — do not surveil non-members private data — remains intact. AIS broadcast data is public by regulation and already commercially aggregated worldwide.*

Generated with [Claude Code](https://claude.ai/code)
