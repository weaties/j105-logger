"""Semantic layer — machine-readable domain knowledge for HelmLog.

Consolidates the implicit sailing and instrument semantics that are currently
scattered across the codebase into structured, queryable definitions. This
module is the "context store" that bridges raw data and AI agent understanding.

Inspired by the agent-native data platform concept: an AI agent querying
HelmLog data needs to know that ``winds.reference=4`` means north-referenced
true wind direction, that TWA is folded to [0, 180] for polar analysis, and
that a "good tack" means BSP loss < 0.5 kts. This module codifies all of that.

No database access, no hardware dependencies. Pure domain definitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


class Unit(Enum):
    """Physical units used in the instrument data."""

    KNOTS = "kts"
    METERS_PER_SECOND = "m/s"
    DEGREES_TRUE = "deg_true"
    DEGREES = "deg"
    METERS = "m"
    CELSIUS = "°C"
    HECTOPASCALS = "hPa"
    DECIMAL_DEGREES = "dec_deg"
    SECONDS = "s"
    DIMENSIONLESS = ""


# ---------------------------------------------------------------------------
# Wind reference
# ---------------------------------------------------------------------------


class WindReference(Enum):
    """Wind angle reference frame (winds.reference column)."""

    BOAT_TRUE = 0  # wind_angle_deg IS the TWA (true wind, boat-referenced)
    APPARENT = 2  # apparent wind — before true-wind correction
    NORTH_TRUE = 4  # wind_angle_deg is TWD; requires heading to derive TWA

    @property
    def description(self) -> str:
        return _WIND_REF_DESC[self]

    @property
    def usable_for_polar(self) -> bool:
        """Only boat-referenced and north-referenced true wind are valid for polar/maneuver."""
        return self in (WindReference.BOAT_TRUE, WindReference.NORTH_TRUE)


_WIND_REF_DESC = {
    WindReference.BOAT_TRUE: (
        "True Wind Angle (TWA) relative to the bow. 0° = head-to-wind, "
        "180° = dead downwind. Already in boat reference frame."
    ),
    WindReference.APPARENT: (
        "Apparent wind as measured by the masthead unit. Combines true wind "
        "with boat speed and heading. Must be corrected to true wind before "
        "use in performance analysis."
    ),
    WindReference.NORTH_TRUE: (
        "True Wind Direction (TWD) as compass bearing from north. Common "
        "B&G fallback. Convert to TWA using: TWA = (TWD - HDG + 360) % 360, "
        "then fold to [0, 180]."
    ),
}


# ---------------------------------------------------------------------------
# Sailing state
# ---------------------------------------------------------------------------


class PointOfSail(Enum):
    """Sailing state derived from True Wind Angle."""

    CLOSE_HAULED = "close_hauled"
    CLOSE_REACH = "close_reach"
    BEAM_REACH = "beam_reach"
    BROAD_REACH = "broad_reach"
    RUNNING = "running"

    @property
    def twa_range(self) -> tuple[float, float]:
        """TWA range in degrees [low, high) for this point of sail."""
        return _POINT_OF_SAIL_RANGES[self]

    @property
    def upwind(self) -> bool:
        return self in (PointOfSail.CLOSE_HAULED, PointOfSail.CLOSE_REACH)


_POINT_OF_SAIL_RANGES: dict[PointOfSail, tuple[float, float]] = {
    PointOfSail.CLOSE_HAULED: (0, 50),
    PointOfSail.CLOSE_REACH: (50, 70),
    PointOfSail.BEAM_REACH: (70, 110),
    PointOfSail.BROAD_REACH: (110, 150),
    PointOfSail.RUNNING: (150, 180),
}


def point_of_sail(twa_deg: float) -> PointOfSail:
    """Classify a folded TWA [0, 180] into point of sail."""
    for pos, (lo, hi) in _POINT_OF_SAIL_RANGES.items():
        if lo <= twa_deg < hi:
            return pos
    return PointOfSail.RUNNING  # 180° exactly


# ---------------------------------------------------------------------------
# Session types
# ---------------------------------------------------------------------------


class SessionType(Enum):
    """Session classification values (races.session_type column)."""

    RACE = "race"
    PRACTICE = "practice"
    DELIVERY = "delivery"
    UNKNOWN = "unknown"
    SYNTHESIZED = "synthesized"
    DEBRIEF = "debrief"

    @property
    def has_instrument_data(self) -> bool:
        return self != SessionType.DEBRIEF

    @property
    def competitive(self) -> bool:
        return self == SessionType.RACE


# ---------------------------------------------------------------------------
# Maneuver types
# ---------------------------------------------------------------------------


class ManeuverType(Enum):
    """Maneuver classification (maneuvers.type column)."""

    TACK = "tack"
    GYBE = "gybe"
    ROUNDING = "rounding"
    MANEUVER = "maneuver"  # heading change that doesn't fit tack/gybe/rounding

    @property
    def description(self) -> str:
        return _MANEUVER_DESC[self]


_MANEUVER_DESC = {
    ManeuverType.TACK: (
        "Heading change while upwind (TWA < 90°) on both sides of the event. "
        "The bow passes through the wind."
    ),
    ManeuverType.GYBE: (
        "Heading change while downwind (TWA > 90°) on both sides. "
        "The stern passes through the wind."
    ),
    ManeuverType.ROUNDING: (
        "Heading change where the boat crosses the 90° TWA boundary — "
        "transitioning between upwind and downwind legs. Indicates a mark rounding."
    ),
    ManeuverType.MANEUVER: (
        "Significant heading change (≥60°) that doesn't classify as tack, gybe, "
        "or rounding. May be a course correction or obstacle avoidance."
    ),
}


# ---------------------------------------------------------------------------
# Instrument field definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldDef:
    """Machine-readable definition of a single data field."""

    table: str
    column: str
    unit: Unit
    description: str
    value_range: tuple[float | None, float | None] = (None, None)
    nullable: bool = False
    semantic_notes: str = ""


# Every instrument field an agent might encounter, with full context.
FIELD_CATALOG: dict[str, FieldDef] = {
    "heading": FieldDef(
        table="headings",
        column="heading_deg",
        unit=Unit.DEGREES_TRUE,
        description="Vessel compass heading — direction the bow points.",
        value_range=(0, 360),
        semantic_notes=(
            "Compass bearing from true north. 0° = north, 90° = east. "
            "Combined with TWD to derive TWA when wind reference is north-based."
        ),
    ),
    "boat_speed": FieldDef(
        table="speeds",
        column="speed_kts",
        unit=Unit.KNOTS,
        description="Speed through water from hull transducer.",
        value_range=(0, 30),
        semantic_notes=(
            "Speed through the water, NOT over the ground. Affected by current. "
            "Key input to polar analysis. Typical racing range: 3–12 kts for a "
            "cruiser-racer; up to 25+ kts for high-performance boats."
        ),
    ),
    "sog": FieldDef(
        table="cogsog",
        column="sog_kts",
        unit=Unit.KNOTS,
        description="Speed over ground from GPS.",
        value_range=(0, 40),
        semantic_notes=(
            "GPS-derived ground speed. BSP ≠ SOG when current is present. "
            "SOG - BSP approximates current component along heading."
        ),
    ),
    "cog": FieldDef(
        table="cogsog",
        column="cog_deg",
        unit=Unit.DEGREES_TRUE,
        description="Course over ground from GPS.",
        value_range=(0, 360),
        semantic_notes=(
            "GPS track direction. Differs from heading by leeway and current. "
            "COG - HDG is an approximation of combined leeway + current set."
        ),
    ),
    "depth": FieldDef(
        table="depths",
        column="depth_m",
        unit=Unit.METERS,
        description="Water depth below transducer.",
        value_range=(0, 500),
        semantic_notes=(
            "Raw transducer reading. Add offset_m to get depth below keel. "
            "Relevant for tactical decisions near shore or in shallow racing areas."
        ),
    ),
    "wind_speed": FieldDef(
        table="winds",
        column="wind_speed_kts",
        unit=Unit.KNOTS,
        description="Wind speed (true or apparent per reference field).",
        value_range=(0, 80),
        semantic_notes=(
            "Interpret with winds.reference: 0 = true wind speed (boat frame), "
            "2 = apparent wind speed, 4 = true wind speed (north frame). "
            "For performance analysis, only reference ∈ {0, 4} should be used."
        ),
    ),
    "wind_angle": FieldDef(
        table="winds",
        column="wind_angle_deg",
        unit=Unit.DEGREES,
        description="Wind angle (meaning depends on reference field).",
        value_range=(0, 360),
        semantic_notes=(
            "reference=0: TWA (bow-relative, 0–360°, fold to [0,180] for polar). "
            "reference=2: AWA (apparent angle from bow). "
            "reference=4: TWD (compass bearing wind blows FROM; subtract heading for TWA)."
        ),
    ),
    "latitude": FieldDef(
        table="positions",
        column="latitude_deg",
        unit=Unit.DECIMAL_DEGREES,
        description="GPS latitude, positive = North.",
        value_range=(-90, 90),
    ),
    "longitude": FieldDef(
        table="positions",
        column="longitude_deg",
        unit=Unit.DECIMAL_DEGREES,
        description="GPS longitude, positive = East.",
        value_range=(-180, 180),
    ),
    "water_temp": FieldDef(
        table="environmental",
        column="water_temp_c",
        unit=Unit.CELSIUS,
        description="Sea surface water temperature.",
        value_range=(-2, 40),
        semantic_notes="Useful for identifying current boundaries and thermal layers.",
    ),
    "wx_wind_speed": FieldDef(
        table="weather",
        column="wind_speed_kts",
        unit=Unit.KNOTS,
        description="Synoptic wind speed from Open-Meteo (hourly).",
        semantic_notes=(
            "Coarse hourly resolution from weather model, NOT the boat's instruments. "
            "Useful as regional context but should not replace onboard TWS for analysis."
        ),
    ),
    "wx_wind_dir": FieldDef(
        table="weather",
        column="wind_dir_deg",
        unit=Unit.DEGREES_TRUE,
        description="Synoptic wind direction from Open-Meteo (hourly).",
        semantic_notes="Direction wind blows FROM, like TWD. Hourly resolution.",
    ),
    "tide_height": FieldDef(
        table="tides",
        column="height_m",
        unit=Unit.METERS,
        description="Tide height above MLLW (Mean Lower Low Water) from NOAA.",
        semantic_notes=(
            "Hourly from nearest NOAA CO-OPS station. Affects depth and current. "
            "Rate of change between readings approximates tidal current strength."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Thresholds & domain constants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Threshold:
    """A named domain threshold with context for why it matters."""

    name: str
    value: float
    unit: Unit
    description: str
    source_module: str


# Consolidated from maneuver_detector.py, polar.py, race_classifier.py
THRESHOLDS: dict[str, Threshold] = {
    "maneuver_heading_change": Threshold(
        name="maneuver_heading_change",
        value=60.0,
        unit=Unit.DEGREES_TRUE,
        description="Minimum heading change to trigger maneuver detection.",
        source_module="maneuver_detector",
    ),
    "maneuver_detection_window": Threshold(
        name="maneuver_detection_window",
        value=15,
        unit=Unit.SECONDS,
        description="Sliding window for accumulating heading changes.",
        source_module="maneuver_detector",
    ),
    "bsp_recovery_fraction": Threshold(
        name="bsp_recovery_fraction",
        value=0.90,
        unit=Unit.DIMENSIONLESS,
        description=(
            "Fraction of pre-maneuver BSP at which the boat is considered recovered. "
            "A tack with BSP loss that never recovers to 90% is flagged as incomplete."
        ),
        source_module="maneuver_detector",
    ),
    "polar_min_sessions": Threshold(
        name="polar_min_sessions",
        value=3,
        unit=Unit.DIMENSIONLESS,
        description=(
            "Minimum distinct race sessions contributing to a (TWS, TWA) bin "
            "before the polar baseline is considered reliable."
        ),
        source_module="polar",
    ),
    "polar_twa_bin_size": Threshold(
        name="polar_twa_bin_size",
        value=5,
        unit=Unit.DEGREES,
        description="TWA bin width for polar baseline. Bins are [0, 5), [5, 10), ... [175, 180).",
        source_module="polar",
    ),
    "polar_tws_bin_size": Threshold(
        name="polar_tws_bin_size",
        value=1,
        unit=Unit.KNOTS,
        description="TWS bin width for polar baseline. Bins are integer knots (floor).",
        source_module="polar",
    ),
    "upwind_downwind_boundary": Threshold(
        name="upwind_downwind_boundary",
        value=90.0,
        unit=Unit.DEGREES,
        description=(
            "TWA boundary: < 90° = upwind, > 90° = downwind. Used for tack/gybe classification."
        ),
        source_module="maneuver_detector",
    ),
    "race_min_duration_min": Threshold(
        name="race_min_duration_min",
        value=30,
        unit=Unit.DIMENSIONLESS,
        description="Minimum session duration (minutes) to classify as a race.",
        source_module="race_classifier",
    ),
    "race_max_duration_min": Threshold(
        name="race_max_duration_min",
        value=300,
        unit=Unit.DIMENSIONLESS,
        description=(
            "Maximum session duration (minutes) for race classification; longer → delivery."
        ),
        source_module="race_classifier",
    ),
    "race_min_speed_kts": Threshold(
        name="race_min_speed_kts",
        value=3.0,
        unit=Unit.KNOTS,
        description="Minimum median SOG for a session to be considered racing.",
        source_module="race_classifier",
    ),
}


# ---------------------------------------------------------------------------
# Wind bands (human-friendly groupings used in analysis)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindBand:
    """Named wind speed range for analysis and discussion."""

    name: str
    tws_range: tuple[float, float]  # [low, high) in knots
    description: str

    def contains(self, tws_kts: float) -> bool:
        return self.tws_range[0] <= tws_kts < self.tws_range[1]


WIND_BANDS: list[WindBand] = [
    WindBand("drifter", (0, 4), "Drifting conditions. Crew weight placement critical."),
    WindBand("light", (4, 8), "Light air. Smooth crew movement, keep the boat heeling slightly."),
    WindBand("medium", (8, 14), "Target conditions for most boats. Best VMG likely."),
    WindBand("fresh", (14, 20), "Fresh breeze. Hiking or trapezing required. Reefing threshold."),
    WindBand("heavy", (20, 30), "Heavy air. Shortened course likely. Survival tactics."),
    WindBand("storm", (30, float("inf")), "Storm conditions. Racing unlikely."),
]


def wind_band_for(tws_kts: float) -> WindBand | None:
    """Return the wind band containing the given TWS."""
    for band in WIND_BANDS:
        if band.contains(tws_kts):
            return band
    return None


# ---------------------------------------------------------------------------
# Derived quantity recipes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivedQuantity:
    """How to compute a derived sailing metric from raw instrument data."""

    name: str
    description: str
    formula: str
    inputs: list[str]
    output_unit: Unit
    caveats: str = ""


DERIVED_QUANTITIES: dict[str, DerivedQuantity] = {
    "twa_from_twd": DerivedQuantity(
        name="twa_from_twd",
        description="Convert north-referenced TWD to boat-referenced TWA.",
        formula="TWA = (TWD - HDG + 360) % 360; if TWA > 180: TWA = 360 - TWA",
        inputs=["wind_angle (reference=4)", "heading"],
        output_unit=Unit.DEGREES,
        caveats="Only valid when wind reference=4. Fold result to [0, 180] for polar use.",
    ),
    "upwind_vmg": DerivedQuantity(
        name="upwind_vmg",
        description="Velocity Made Good toward the wind.",
        formula="VMG = BSP × cos(TWA)",
        inputs=["boat_speed", "twa (folded, 0–180°)"],
        output_unit=Unit.KNOTS,
        caveats="Only meaningful when TWA < 90° (upwind). Higher is better.",
    ),
    "downwind_vmg": DerivedQuantity(
        name="downwind_vmg",
        description="Velocity Made Good toward the downwind mark.",
        formula="VMG = BSP × cos(180° - TWA)",
        inputs=["boat_speed", "twa (folded, 0–180°)"],
        output_unit=Unit.KNOTS,
        caveats="Only meaningful when TWA > 90° (downwind). Higher is better.",
    ),
    "bsp_delta": DerivedQuantity(
        name="bsp_delta",
        description="Boat speed relative to polar baseline.",
        formula="delta = BSP - baseline_mean_bsp(TWS_bin, TWA_bin)",
        inputs=["boat_speed", "wind_speed", "wind_angle"],
        output_unit=Unit.KNOTS,
        caveats=(
            "Positive = outperforming baseline, negative = underperforming. "
            "Baseline requires ≥3 sessions in the (TWS, TWA) bin to be reliable."
        ),
    ),
    "maneuver_bsp_loss": DerivedQuantity(
        name="maneuver_bsp_loss",
        description="Speed lost during a tack or gybe.",
        formula="loss = pre_baseline_bsp - min(bsp during maneuver)",
        inputs=["boat_speed (30s pre-window)", "boat_speed (during event)"],
        output_unit=Unit.KNOTS,
        caveats=(
            "Pre-baseline is the mean BSP in the 30 seconds before the heading change. "
            "Lower loss = better maneuver execution."
        ),
    ),
    "current_estimate": DerivedQuantity(
        name="current_estimate",
        description="Rough current speed/direction from BSP vs SOG.",
        formula="current_speed ≈ |SOG - BSP|; current_set ≈ COG - HDG",
        inputs=["boat_speed", "sog", "heading", "cog"],
        output_unit=Unit.KNOTS,
        caveats=(
            "Very rough approximation. Assumes leeway is small. "
            "Better estimates require multiple headings to separate leeway from current."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Table relationship map
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableJoin:
    """How two tables relate for query purposes."""

    left_table: str
    right_table: str
    join_method: str
    description: str


TABLE_JOINS: list[TableJoin] = [
    TableJoin(
        "races",
        "headings|speeds|depths|positions|cogsog|winds|environmental",
        "races.start_utc <= instrument.ts <= races.end_utc "
        "(AND instrument.race_id = races.id for synthesized sessions)",
        "Session → instrument data. Timestamp range join, with race_id for synthesized data.",
    ),
    TableJoin(
        "winds",
        "headings",
        "Same-second timestamp join (ts[:19] truncation)",
        "Required to convert reference=4 (TWD) to TWA using heading.",
    ),
    TableJoin(
        "polar_baseline",
        "winds|speeds",
        "Match live (TWS, TWA) to polar_baseline.(tws_bin, twa_bin)",
        "Performance comparison: bin the live wind conditions, look up baseline BSP.",
    ),
    TableJoin(
        "weather",
        "races",
        "Hourly alignment: weather.ts rounded to hour within session window",
        "Regional weather context — coarser resolution than onboard instruments.",
    ),
    TableJoin(
        "tides",
        "races",
        "Hourly alignment: tides.ts within session window",
        "Tidal context for shallow-water venues. Rate of change ≈ current strength.",
    ),
    TableJoin(
        "audio_sessions",
        "races",
        "audio_sessions.race_id = races.id",
        "Audio recording linked to a sailing session.",
    ),
    TableJoin(
        "transcripts",
        "audio_sessions",
        "transcripts.audio_session_id = audio_sessions.id",
        "Transcript text and speaker segments for a recording.",
    ),
    TableJoin(
        "maneuvers",
        "races",
        "maneuvers.race_id = races.id",
        "Detected maneuvers for a session. Each has a timestamp for instrument correlation.",
    ),
]


# ---------------------------------------------------------------------------
# Query helpers — the semantic layer as a function
# ---------------------------------------------------------------------------


def lookup_field(name: str) -> FieldDef | None:
    """Look up a field definition by common name."""
    return FIELD_CATALOG.get(name)


def lookup_threshold(name: str) -> Threshold | None:
    """Look up a domain threshold by name."""
    return THRESHOLDS.get(name)


def lookup_derived(name: str) -> DerivedQuantity | None:
    """Look up a derived quantity recipe by name."""
    return DERIVED_QUANTITIES.get(name)


def describe_wind_reference(ref: int) -> str:
    """Explain what a wind reference code means."""
    try:
        return WindReference(ref).description
    except ValueError:
        return f"Unknown wind reference code: {ref}"


# ---------------------------------------------------------------------------
# Full catalog export (for agent consumption)
# ---------------------------------------------------------------------------


def catalog_as_dict() -> dict[str, Any]:
    """Export the entire semantic layer as a JSON-serializable dict.

    Intended for feeding to an LLM or agent as context, or for building
    a machine-readable schema document.
    """
    return {
        "fields": {
            k: {
                "table": f.table,
                "column": f.column,
                "unit": f.unit.value,
                "description": f.description,
                "value_range": list(f.value_range),
                "semantic_notes": f.semantic_notes,
            }
            for k, f in FIELD_CATALOG.items()
        },
        "wind_references": {
            wr.value: {"name": wr.name, "description": wr.description} for wr in WindReference
        },
        "points_of_sail": {
            pos.value: {
                "twa_range": list(pos.twa_range),
                "upwind": pos.upwind,
            }
            for pos in PointOfSail
        },
        "session_types": {
            st.value: {
                "has_instrument_data": st.has_instrument_data,
                "competitive": st.competitive,
            }
            for st in SessionType
        },
        "maneuver_types": {mt.value: {"description": mt.description} for mt in ManeuverType},
        "thresholds": {
            k: {
                "value": t.value,
                "unit": t.unit.value,
                "description": t.description,
                "source_module": t.source_module,
            }
            for k, t in THRESHOLDS.items()
        },
        "wind_bands": [
            {
                "name": b.name,
                "tws_range": list(b.tws_range),
                "description": b.description,
            }
            for b in WIND_BANDS
        ],
        "derived_quantities": {
            k: {
                "description": d.description,
                "formula": d.formula,
                "inputs": d.inputs,
                "output_unit": d.output_unit.value,
                "caveats": d.caveats,
            }
            for k, d in DERIVED_QUANTITIES.items()
        },
        "table_joins": [
            {
                "left": j.left_table,
                "right": j.right_table,
                "method": j.join_method,
                "description": j.description,
            }
            for j in TABLE_JOINS
        ],
    }
