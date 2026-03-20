# Spec: Semantic Layer — Machine-Readable Domain Knowledge

**Status:** Draft
**Risk Tier:** Standard (new module, no changes to Critical/High tier code)
**Related:** [Data Engineering for Machine Users](https://gradientflow.substack.com/p/data-engineering-for-machine-users)

---

## Problem

HelmLog has 50+ SQLite tables with rich 1 Hz sailing data, audio transcripts,
maneuver detections, and polar baselines. But the domain knowledge needed to
*interpret* this data is scattered implicitly across the codebase:

- Wind reference codes (0/2/4) are magic numbers in `nmea2000.py:146`
- The upwind/downwind boundary (TWA = 90°) lives in `maneuver_detector.py:329`
- Polar bin sizes (5° TWA, 1 kt TWS) are constants in `polar.py:25-29`
- Unit conventions (knots, metres, Celsius) are in dataclass docstrings
- Table join patterns are implicit in query code across `export.py`, `web.py`

An AI agent querying HelmLog data hits the "context gap" — it has access to
data but lacks the business logic to interpret it correctly.

## Solution

A **semantic layer** module (`semantic_layer.py`) that consolidates all implicit
domain knowledge into structured, queryable Python definitions. Pure data — no
database access, no hardware dependencies. Serves as the "context store" for
both human developers and AI agents.

---

## Decision Table: Semantic Layer Components

Each row is an independent component. All are Phase 1 (this PR).

| Component | Data Structure | Purpose | Consumers |
|---|---|---|---|
| Field catalog | `dict[str, FieldDef]` | Table, column, unit, range, semantic notes for every instrument field | Agent context, documentation |
| Wind reference enum | `WindReference(Enum)` | Explain reference codes (0/2/4), flag polar-usability | Agent context, polar.py, maneuver_detector.py |
| Point of sail | `PointOfSail(Enum)` | TWA → sailing state (close-hauled through running) | Agent context, analysis plugins |
| Session types | `SessionType(Enum)` | Session classification with instrument-data/competitive flags | Agent context, race_classifier.py |
| Maneuver types | `ManeuverType(Enum)` | Tack/gybe/rounding/maneuver with descriptions | Agent context, maneuver_detector.py |
| Threshold registry | `dict[str, Threshold]` | All magic numbers with source module attribution | Agent context, cross-module documentation |
| Wind bands | `list[WindBand]` | Named wind ranges (drifter → storm) with tactical context | Agent context, analysis plugins |
| Derived quantities | `dict[str, DerivedQuantity]` | Computation recipes: formula, inputs, unit, caveats | Agent query generation |
| Table joins | `list[TableJoin]` | How tables relate (timestamp ranges, foreign keys) | Agent query generation |
| Catalog export | `catalog_as_dict() → dict` | Full JSON-serializable dump of all above | LLM context window, API endpoint |

## Decision Table: Wind Reference Interpretation

This is the most common source of confusion for both humans and agents.

| `winds.reference` | Name | `wind_angle_deg` means | Usable for polar? | Conversion to TWA |
|---|---|---|---|---|
| 0 | Boat-referenced true | TWA (angle from bow) | Yes | Direct (fold to [0, 180]) |
| 2 | Apparent | AWA (apparent angle) | **No** | Requires BSP + heading |
| 4 | North-referenced true | TWD (compass bearing) | Yes | TWA = (TWD - HDG + 360) % 360, fold |

## Decision Table: Point of Sail Classification

| Point of Sail | TWA Range | Upwind? | Typical Use |
|---|---|---|---|
| Close-hauled | [0°, 50°) | Yes | Beating to windward mark |
| Close reach | [50°, 70°) | Yes | Offset legs, reaching starts |
| Beam reach | [70°, 110°) | No | Cross-course legs |
| Broad reach | [110°, 150°) | No | VMG running angles |
| Running | [150°, 180°] | No | Dead downwind, DDW legs |

## State Diagram: Data Flow for Agent Queries

```
                    ┌─────────────────────┐
                    │   Natural Language   │
                    │       Query          │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │   Semantic Layer     │
                    │  (context store)     │
                    │                     │
                    │  • Field catalog    │
                    │  • Thresholds       │
                    │  • Derived recipes  │
                    │  • Table joins      │
                    └──────┬──────┬───────┘
                           │      │
              ┌────────────┘      └────────────┐
              ▼                                ▼
    ┌──────────────────┐             ┌──────────────────┐
    │  Structured Query │             │  Embedding Query  │
    │  (SQL via SQLite) │             │  (sqlite-vec)     │
    │                   │             │                   │
    │  instruments,     │             │  transcripts,     │
    │  maneuvers,       │             │  boat_settings,   │
    │  polar baseline   │             │  coaching notes   │
    └────────┬─────────┘             └────────┬─────────┘
             │                                │
             └────────────┬───────────────────┘
                          ▼
                ┌──────────────────┐
                │  Combined Result  │
                │  + Interpretation │
                └──────────────────┘
```

## EARS Requirements (for future phases)

### Phase 2 — Transcript Embeddings

**WHEN** a transcript is completed (status → done),
**THE SYSTEM SHALL** embed each speaker segment using `all-MiniLM-L6-v2`
and store the embedding in the `transcript_embeddings` virtual table
**WITHIN** 60 seconds of transcript completion.

**WHEN** a user issues a semantic search query,
**THE SYSTEM SHALL** return the top-K matching transcript segments
with their timestamps, speaker labels, and linked session metadata.

**WHILE** embedding segments,
**THE SYSTEM SHALL NOT** embed segments shorter than 5 words
(insufficient semantic content).

### Phase 3 — Agent Query Interface

**WHEN** an agent submits a natural language question,
**THE SYSTEM SHALL** inject `catalog_as_dict()` as context
and generate a SQL query that references correct tables, columns, and units.

**WHEN** a query references wind data,
**THE SYSTEM SHALL** automatically filter to `reference IN (0, 4)`
unless the query explicitly asks for apparent wind.

**WHEN** a query references polar performance,
**THE SYSTEM SHALL** only return bins with `session_count >= 3`
and note the confidence level in the response.

---

## Verification

### Phase 1 (this PR)

- [x] `semantic_layer.py` passes `ruff check`, `ruff format`, `mypy`
- [x] 35 tests covering all enums, lookups, catalog export, JSON serialization
- [x] 99% code coverage on the module
- [x] `catalog_as_dict()` produces valid JSON with all 9 sections
- [x] No changes to existing modules (pure additive)
- [x] Module is importable without hardware or database

### Future phases

- Phase 2: transcript embedding tests with mock segments
- Phase 3: agent query integration tests with known-answer questions
