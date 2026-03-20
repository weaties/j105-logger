"""Tests for the semantic layer — domain knowledge definitions."""

from __future__ import annotations

from helmlog.semantic_layer import (
    DERIVED_QUANTITIES,
    FIELD_CATALOG,
    THRESHOLDS,
    ManeuverType,
    PointOfSail,
    SessionType,
    Unit,
    WindReference,
    catalog_as_dict,
    describe_wind_reference,
    lookup_derived,
    lookup_field,
    lookup_threshold,
    point_of_sail,
    wind_band_for,
)


class TestWindReference:
    def test_boat_true_is_polar_usable(self) -> None:
        assert WindReference.BOAT_TRUE.usable_for_polar is True

    def test_apparent_not_polar_usable(self) -> None:
        assert WindReference.APPARENT.usable_for_polar is False

    def test_north_true_is_polar_usable(self) -> None:
        assert WindReference.NORTH_TRUE.usable_for_polar is True

    def test_values_match_database(self) -> None:
        assert WindReference.BOAT_TRUE.value == 0
        assert WindReference.APPARENT.value == 2
        assert WindReference.NORTH_TRUE.value == 4

    def test_describe_known_reference(self) -> None:
        desc = describe_wind_reference(0)
        assert "TWA" in desc
        assert "bow" in desc.lower() or "boat" in desc.lower()

    def test_describe_unknown_reference(self) -> None:
        desc = describe_wind_reference(99)
        assert "Unknown" in desc


class TestPointOfSail:
    def test_close_hauled(self) -> None:
        assert point_of_sail(30) == PointOfSail.CLOSE_HAULED
        assert PointOfSail.CLOSE_HAULED.upwind is True

    def test_beam_reach(self) -> None:
        assert point_of_sail(90) == PointOfSail.BEAM_REACH
        assert PointOfSail.BEAM_REACH.upwind is False

    def test_running(self) -> None:
        assert point_of_sail(170) == PointOfSail.RUNNING

    def test_exact_180_is_running(self) -> None:
        assert point_of_sail(180) == PointOfSail.RUNNING

    def test_broad_reach(self) -> None:
        assert point_of_sail(130) == PointOfSail.BROAD_REACH

    def test_close_reach(self) -> None:
        assert point_of_sail(60) == PointOfSail.CLOSE_REACH


class TestSessionType:
    def test_debrief_no_instrument_data(self) -> None:
        assert SessionType.DEBRIEF.has_instrument_data is False

    def test_race_has_instrument_data(self) -> None:
        assert SessionType.RACE.has_instrument_data is True

    def test_only_race_is_competitive(self) -> None:
        assert SessionType.RACE.competitive is True
        assert SessionType.PRACTICE.competitive is False


class TestManeuverType:
    def test_all_have_descriptions(self) -> None:
        for mt in ManeuverType:
            assert len(mt.description) > 10

    def test_tack_description_mentions_upwind(self) -> None:
        assert "upwind" in ManeuverType.TACK.description.lower()


class TestFieldCatalog:
    def test_lookup_known_field(self) -> None:
        f = lookup_field("boat_speed")
        assert f is not None
        assert f.table == "speeds"
        assert f.unit == Unit.KNOTS

    def test_lookup_unknown_returns_none(self) -> None:
        assert lookup_field("nonexistent") is None

    def test_all_fields_have_descriptions(self) -> None:
        for name, f in FIELD_CATALOG.items():
            assert f.description, f"{name} has no description"
            assert f.table, f"{name} has no table"
            assert f.column, f"{name} has no column"

    def test_wind_fields_mention_reference(self) -> None:
        ws = lookup_field("wind_speed")
        assert ws is not None
        assert "reference" in ws.semantic_notes.lower()


class TestThresholds:
    def test_lookup_known_threshold(self) -> None:
        t = lookup_threshold("maneuver_heading_change")
        assert t is not None
        assert t.value == 60.0

    def test_polar_min_sessions_is_three(self) -> None:
        t = lookup_threshold("polar_min_sessions")
        assert t is not None
        assert t.value == 3

    def test_upwind_boundary_is_90(self) -> None:
        t = lookup_threshold("upwind_downwind_boundary")
        assert t is not None
        assert t.value == 90.0

    def test_all_have_source_module(self) -> None:
        for name, t in THRESHOLDS.items():
            assert t.source_module, f"{name} has no source_module"


class TestWindBands:
    def test_light_air(self) -> None:
        band = wind_band_for(6)
        assert band is not None
        assert band.name == "light"

    def test_heavy(self) -> None:
        band = wind_band_for(25)
        assert band is not None
        assert band.name == "heavy"

    def test_zero_is_drifter(self) -> None:
        band = wind_band_for(0)
        assert band is not None
        assert band.name == "drifter"

    def test_full_coverage_to_50(self) -> None:
        """Every whole knot from 0–50 maps to a band."""
        for tws in range(51):
            assert wind_band_for(float(tws)) is not None


class TestDerivedQuantities:
    def test_lookup_vmg(self) -> None:
        d = lookup_derived("upwind_vmg")
        assert d is not None
        assert "cos" in d.formula.lower()

    def test_all_have_formulas(self) -> None:
        for name, d in DERIVED_QUANTITIES.items():
            assert d.formula, f"{name} has no formula"
            assert d.inputs, f"{name} has no inputs"


class TestCatalogExport:
    def test_catalog_is_serializable(self) -> None:
        """The full catalog should be JSON-serializable (no custom objects)."""
        import json

        data = catalog_as_dict()
        # Will raise if not serializable
        json.dumps(data)

    def test_catalog_has_all_sections(self) -> None:
        data = catalog_as_dict()
        expected_keys = {
            "fields",
            "wind_references",
            "points_of_sail",
            "session_types",
            "maneuver_types",
            "thresholds",
            "wind_bands",
            "derived_quantities",
            "table_joins",
        }
        assert set(data.keys()) == expected_keys

    def test_catalog_fields_match_field_catalog(self) -> None:
        data = catalog_as_dict()
        assert set(data["fields"].keys()) == set(FIELD_CATALOG.keys())

    def test_catalog_thresholds_match(self) -> None:
        data = catalog_as_dict()
        assert set(data["thresholds"].keys()) == set(THRESHOLDS.keys())
