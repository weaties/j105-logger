---
name: domain
description: Sailing instrument domain reference — Signal K paths, NMEA 2000 PGNs, instrument relationships, racing concepts, and calibration parameters. Auto-triggers when working on sk_reader.py, can_reader.py, nmea2000.py, polar.py, boat_settings.py, synthesize.py, maneuver_detector.py, export.py, or calibration-related code.
---

# Sailing Instrument Domain Reference

This skill provides authoritative domain knowledge for HelmLog's sailing
instrument system. Use it as a reference when writing or reviewing code that
handles instrument data. **Do not guess** at Signal K paths, PGN numbers, or
instrument relationships — consult this reference.

---

## 1. Instrument Relationships

### The Core Seven

| Abbreviation | Full Name | Source | Units | What It Measures |
|---|---|---|---|---|
| **HDG** | Heading (True) | Compass + variation | degrees (0-360) | Direction the bow points, relative to true north |
| **BSP** | Boat Speed (Through Water) | Paddlewheel / ultrasonic | knots | Speed through the water (affected by current) |
| **SOG** | Speed Over Ground | GPS | knots | Speed relative to the earth's surface |
| **COG** | Course Over Ground | GPS | degrees (0-360) | Track direction relative to true north |
| **TWS** | True Wind Speed | Derived | knots | Wind speed relative to the water/ground |
| **TWA** | True Wind Angle | Derived | degrees (0-180) | Angle of true wind relative to bow (0 = headwind) |
| **AWA** | Apparent Wind Angle | Masthead unit | degrees (0-180) | Angle of wind as felt on the boat |
| **AWS** | Apparent Wind Speed | Masthead unit | knots | Wind speed as felt on the boat |

### Derived Quantities

| Quantity | Formula | Inputs | Notes |
|---|---|---|---|
| **VMG upwind** | `BSP * cos(TWA)` | BSP, TWA | TWA < 90 degrees. Higher is better. |
| **VMG downwind** | `BSP * cos(180 - TWA)` | BSP, TWA | TWA > 90 degrees. Higher is better. |
| **Apparent wind** | Vector sum of true wind + boat motion | TWS, TWA, BSP | `synthesize.py:apparent_wind()` |
| **TWA from TWD** | `(TWD - HDG + 360) % 360` | TWD, HDG | When wind reference = 4 (north-referenced) |

### How Wind Instruments Relate (Critical)

```
Physical sensors:
  Masthead wand → AWA, AWS (what the crew sees)
  GPS           → SOG, COG
  Compass       → HDG
  Paddlewheel   → BSP

Derivation chain:
  AWA + AWS + BSP → TWA + TWS  (instrument processor does this)
                                 NOT the other way around.

  TWA is derived FROM apparent wind, not the reverse.
  The instrument processor (B&G Zeus) resolves true from apparent.
```

**Common mistakes the model makes:**
- Confusing `speedThroughWater` (BSP, for polars) with `speedOverGround` (SOG, for navigation). **Polar calculations always use BSP.**
- Thinking TWA is an input to AWA. It's the opposite: AWA is measured, TWA is derived.
- Treating TWA as 0-360. TWA is 0-180 in HelmLog (port/starboard symmetry assumed for polars).

### Wind Reference Codes (PGN 130306)

| Code | Name | `wind_angle_deg` Means | Used In Polars? |
|---|---|---|---|
| **0** | True, boat-referenced | TWA directly (0 = headwind) | Yes |
| **2** | Apparent | AWA (0 = head-to-wind) | No — filtered out |
| **4** | True, north-referenced | TWD (compass direction wind blows FROM) | Yes, after `TWA = (TWD - HDG) % 360` |

**B&G systems typically emit reference 4** (TWD), not reference 0 (TWA). Code
that computes TWA must check the reference field and apply heading correction
when reference = 4. See `polar.py:_compute_twa()`.

---

## 2. Signal K Path Reference

These are the actual paths HelmLog reads from Signal K Server. All paths are
under the `vessels.self` context.

### Simple Paths (single value per update)

| Signal K Path | PGN | Record Type | SK Units | Conversion | Output Field |
|---|---|---|---|---|---|
| `navigation.headingTrue` | 127250 | `HeadingRecord` | radians | x 180/pi | `heading_deg` |
| `navigation.speedThroughWater` | 128259 | `SpeedRecord` | m/s | x 1.94384449 | `speed_kts` |
| `environment.depth.belowKeel` | 128267 | `DepthRecord` | metres | none | `depth_m` |
| `environment.water.temperature` | 130310 | `EnvironmentalRecord` | Kelvin | - 273.15 | `water_temp_c` |

### Paired Paths (buffered until both arrive)

**COG + SOG (PGN 129026):**

| Signal K Path | Conversion | Output Field |
|---|---|---|
| `navigation.courseOverGroundTrue` | rad x 180/pi | `cog_deg` |
| `navigation.speedOverGround` | m/s x 1.94384449 | `sog_kts` |

**True Wind (PGN 130306, reference 0 or 4):**

| Signal K Path | Conversion | Reference | Priority |
|---|---|---|---|
| `environment.wind.angleTrue` | rad x 180/pi | 0 (boat) | 1st choice |
| `environment.wind.angleTrueWater` | rad x 180/pi | 0 (boat) | 2nd choice |
| `environment.wind.angleTrueGround` | rad x 180/pi | 0 (boat) | 3rd choice |
| `environment.wind.directionTrue` | rad x 180/pi | 4 (north) | Last resort |
| `environment.wind.speedTrue` | m/s x 1.94384449 | — | Paired with any above |

**Apparent Wind (PGN 130306, reference 2):**

| Signal K Path | Conversion | Output Field |
|---|---|---|
| `environment.wind.angleApparent` | rad x 180/pi | `wind_angle_deg` |
| `environment.wind.speedApparent` | m/s x 1.94384449 | `wind_speed_kts` |

**Position (PGN 129025):**

| Signal K Path | Format | Output Fields |
|---|---|---|
| `navigation.position` | Object: `{latitude, longitude}` | `latitude_deg`, `longitude_deg` |

### Unit Conversion Constants (`sk_reader.py`)

```python
_RAD_TO_DEG = 180.0 / math.pi      # 57.29577951...
_MPS_TO_KTS = 1.94384449           # metres/second to knots
_KELVIN_OFFSET = 273.15            # Kelvin to Celsius
SK_SOURCE_ADDR = 0                 # Source address for SK-originated records
```

### Self-Vessel Filtering

Only deltas with `context == "vessels.self"` are processed. Other-vessel data
(AIS targets, nearby boats) is rejected to prevent data corruption (#208).

---

## 3. NMEA 2000 PGN Reference

Seven PGNs are decoded. All use little-endian byte order.

### PGN 127250 — Vessel Heading

| Bytes | Field | Scale | Units | Not-Available |
|---|---|---|---|---|
| 0 | SID | — | — | — |
| 1-2 | Heading | 0.0001 rad/bit | radians -> degrees | 0xFFFF |
| 3-4 | Deviation | 0.0001 rad/bit | radians -> degrees | 0x7FFF |
| 5-6 | Variation | 0.0001 rad/bit | radians -> degrees | 0x7FFF |
| 7 | Reference | bits 0-1 | 0=true, 1=magnetic | — |

### PGN 128259 — Speed Through Water

| Bytes | Field | Scale | Units | Not-Available |
|---|---|---|---|---|
| 0 | SID | — | — | — |
| 1-2 | Speed | 0.01 m/s per bit | m/s -> knots | 0xFFFF |
| 3-4 | Speed (transducer) | 0.01 m/s per bit | m/s -> knots | — |
| 5 | Speed Type | bits | — | — |

### PGN 128267 — Water Depth

| Bytes | Field | Scale | Units | Not-Available |
|---|---|---|---|---|
| 0 | SID | — | — | — |
| 1-4 | Depth | 0.01 m/bit | metres (below transducer) | 0xFFFFFFFF |
| 5-6 | Offset | 0.001 m/bit (signed) | metres (keel-to-transducer) | 0x8000 |

### PGN 129025 — Position Rapid Update

| Bytes | Field | Scale | Units | Not-Available |
|---|---|---|---|---|
| 0-3 | Latitude | 1e-7 deg/bit (signed) | degrees (+N) | 0x80000000 |
| 4-7 | Longitude | 1e-7 deg/bit (signed) | degrees (+E) | 0x80000000 |

### PGN 129026 — COG & SOG Rapid Update

| Bytes | Field | Scale | Units | Not-Available |
|---|---|---|---|---|
| 0 | SID | — | — | — |
| 1 | COG Reference | bits 0-1 | 0=true, 1=magnetic | — |
| 2-3 | COG | 0.0001 rad/bit | radians -> degrees | 0xFFFF |
| 4-5 | SOG | 0.01 m/s per bit | m/s -> knots | 0xFFFF |
| 6-7 | Reserved | — | — | — |

### PGN 130306 — Wind Data

| Bytes | Field | Scale | Units | Not-Available |
|---|---|---|---|---|
| 0 | SID | — | — | — |
| 1-2 | Wind Speed | 0.01 m/s per bit | m/s -> knots | 0xFFFF |
| 3-4 | Wind Angle | 0.0001 rad/bit | radians -> degrees | 0xFFFF |
| 5 | Reference | bits 0-2 | 0=true, 2=apparent, 4=true north | — |

### PGN 130310 — Environmental Parameters

| Bytes | Field | Scale | Units | Not-Available |
|---|---|---|---|---|
| 0 | SID | — | — | — |
| 1-2 | Water Temp | 0.01 K/bit | Kelvin -> Celsius | 0xFFFF |
| 3-4 | Atm. Pressure | 0.1 hPa/bit | hPa (ignored) | — |
| 5-6 | Reserved | — | — | — |

### Blocked PGNs (AIS — data licensing policy)

PGNs 129038-129810 (AIS and DSC) are **never ingested or stored**. This is a
data licensing requirement (#208), not a technical limitation.

### CAN Bus / J1939 Arbitration ID (29-bit extended)

```
bits 28-26: priority (3 bits, typically 6)
bit  24:    data page (0 or 1)
bits 23-16: PDU format (PF)
bits 15-8:  PDU specific (PS)
bits  7-0:  source address
```

PGN extraction: for PDU2 (PF >= 240, broadcast), `PGN = (data_page << 16) | (PF << 8) | PS`.

---

## 4. Racing Concepts

### Race Structure

A **session** is a contiguous period of data recording. Sessions contain
**legs** between **marks**. A mark rounding changes the leg.

### Maneuver Classification (`maneuver_detector.py`)

| TWA Before | TWA After | Heading Change | Classification |
|---|---|---|---|
| < 90 (upwind) | < 90 (upwind) | >= 60 deg | **Tack** |
| > 90 (downwind) | > 90 (downwind) | >= 60 deg | **Gybe** |
| < 90 | > 90 (or vice versa) | >= 60 deg | **Mark rounding** |

### Polar Performance

A **polar** maps `(TWS, TWA) -> BSP` — the expected boat speed for given wind
conditions. HelmLog builds a polar baseline from completed race sessions:

- **TWS bins:** floor of TWS in knots (0, 1, 2, ... 30+)
- **TWA bins:** floor to nearest 5 degrees (0, 5, 10, ... 175, 180)
- **Per bin:** mean BSP, P90 BSP, session count, sample count
- **Minimum sessions:** 3 races before baseline is published

**Polar calculations always use BSP (speed through water), never SOG (speed
over ground).** SOG includes current effects and produces misleading polars.

### VMG (Velocity Made Good)

VMG is the speed component toward the wind (upwind) or away from it (downwind).

- **Upwind VMG** = `BSP * cos(TWA)` where TWA < 90 degrees
- **Downwind VMG** = `BSP * cos(180 - TWA)` where TWA >= 90 degrees
- VMG is analyzed **per sail** and **per wind band** (0-6, 6-10, 10-15, 15-20, 20+ kts)

"Good VMG" upwind means sailing as close to the wind as possible while
maintaining speed. There's an optimal TWA for each wind speed — sailing too
close (pinching) kills BSP; sailing too wide (footing) wastes angle.

### J/105 Reference Polars (`synthesize.py`)

Used for test data generation. Optimal upwind/downwind angles and speeds:

| TWS (kts) | Upwind TWA | Upwind BSP | Downwind TWA | Downwind BSP |
|---|---|---|---|---|
| 6 | 44 deg | 5.2 kts | 150 deg | 4.8 kts |
| 8 | 43 deg | 6.0 kts | 145 deg | 5.8 kts |
| 10 | 42 deg | 6.5 kts | 140 deg | 6.5 kts |
| 12 | 41 deg | 6.8 kts | 135 deg | 7.0 kts |
| 16 | 39 deg | 7.3 kts | 130 deg | 7.6 kts |

---

## 5. Calibration Parameters (`boat_settings.py`)

These are crew-entered tuning parameters, not instrument readings. They don't
feed into calculations (yet) — they're stored for debrief correlation.

### What Miscalibration Looks Like

| Parameter | What It Controls | Miscalibration Symptom |
|---|---|---|
| **BSP calibration** | Paddlewheel scale factor | Polars consistently above/below known target; VMG unreliable |
| **AWA offset** | Wind vane zero point | Upwind performance looks different on port vs starboard tack |
| **Compass deviation** | Heading correction table | TWA wrong when derived from TWD (reference=4); tack/gybe misclassified |
| **Depth offset** | Transducer-to-keel distance | `offset_m` in DepthRecord; shallow-water alarms fire at wrong depth |
| **SOG/COG** | GPS antenna position | Usually accurate; offset matters for match racing (boat length precision) |

### Rig and Sail Controls (Stored in `boat_settings.py`)

**Rig tension:** `shroud_tension_upper`, `shroud_tension_d2`, `shroud_tension_lowers` (Loos units)

**Sail controls:** `main_halyard`, `jib_halyard`, `vang`, `cunningham`, `outhaul`,
`backstay`, `main_sheet_tension`, `jib_sheet_tension_port/starboard`,
`traveler_position` (inches)

**Deck:** `car_position_port`, `car_position_starboard` (hole numbers)

**Conditions:** `weight_distribution` (preset), `swell_height`, `swell_period`, `chop`

---

## 6. Data Flow Summary

```
Signal K Server                  CAN Bus (legacy)
      |                               |
  sk_reader.py                   can_reader.py
      |                               |
      +--- same PGNRecord types ------+
                     |
               storage.py (SQLite)
                     |
      +---------+----+--------+--------+
      |         |             |        |
   polar.py  export.py   web.py   maneuver_detector.py
      |
  analysis/plugins/
  (sail_vmg, polar_baseline)
```

Both data paths produce identical `PGNRecord` dataclasses. Downstream code is
data-source agnostic.

### Storage Tables

| Table | Key Columns | From PGN |
|---|---|---|
| `headings` | ts, heading_deg, deviation_deg, variation_deg | 127250 |
| `speeds` | ts, speed_kts | 128259 |
| `depths` | ts, depth_m, offset_m | 128267 |
| `positions` | ts, latitude_deg, longitude_deg | 129025 |
| `cogsog` | ts, cog_deg, sog_kts | 129026 |
| `winds` | ts, wind_speed_kts, wind_angle_deg, reference | 130306 |
| `environmental` | ts, water_temp_c | 130310 |

All tables indexed by ISO 8601 UTC timestamp, truncated to the second.

### Export Column Mapping

| Export Column | Source Table | Notes |
|---|---|---|
| HDG | headings | heading_deg |
| BSP | speeds | speed_kts |
| COG, SOG | cogsog | cog_deg, sog_kts |
| TWS, TWA | winds (ref 0 or 4) | True wind only; TWA computed if ref=4 |
| AWS, AWA | winds (ref 2) | Apparent wind |
| DEPTH | depths | depth_m |
| LAT, LON | positions | latitude_deg, longitude_deg |
| WTEMP | environmental | water_temp_c |
| BSP_BASELINE | polar baseline | Expected BSP for (TWS_bin, TWA_bin) |
| BSP_DELTA | computed | session_BSP - baseline_BSP |
