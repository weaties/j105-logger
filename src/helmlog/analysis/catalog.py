"""Model catalog lifecycle for Phase 2 (#285).

Tracks the lifecycle of analysis plugins proposed for co-op promotion.

States
------
boat_local     Plugin exists on this boat only (default on discovery).
proposed       Boat has proposed the plugin to a co-op; awaiting moderator decision.
rejected       Moderator rejected the proposal (with reason); can be re-proposed.
co_op_active   Moderator approved; plugin appears in all co-op members' catalog.
co_op_default  Same as co_op_active but marked as the co-op default model.
deprecated     Plugin retired; visible as history but cannot run on new sessions.

State transitions
-----------------
boat_local   → proposed      : propose_to_co_op()
proposed     → co_op_active  : approve()
proposed     → rejected      : reject()
rejected     → proposed      : re-propose via propose_to_co_op()
co_op_active → co_op_default : set_co_op_default()
co_op_default → co_op_active : unset_co_op_default()
co_op_active / co_op_default → deprecated : deprecate()
deprecated   → co_op_active  : restore()
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------

BOAT_LOCAL: str = "boat_local"
PROPOSED: str = "proposed"
REJECTED: str = "rejected"
CO_OP_ACTIVE: str = "co_op_active"
CO_OP_DEFAULT: str = "co_op_default"
DEPRECATED: str = "deprecated"

#: States in which a plugin can be actively run on new sessions.
ACTIVE_STATES: frozenset[str] = frozenset({BOAT_LOCAL, CO_OP_ACTIVE, CO_OP_DEFAULT})

#: Valid states that can be stored in the catalog.
ALL_STATES: frozenset[str] = frozenset(
    {BOAT_LOCAL, PROPOSED, REJECTED, CO_OP_ACTIVE, CO_OP_DEFAULT, DEPRECATED}
)

# PII-derived keywords that must not appear in co-op model outputs.
_PII_TERMS: frozenset[str] = frozenset(
    {"audio", "transcript", "photo", "biometric", "diarized", "notes", "comment"}
)


# ---------------------------------------------------------------------------
# CatalogEntry dataclass
# ---------------------------------------------------------------------------


@dataclass
class CatalogEntry:
    """A record in the analysis_catalog table."""

    plugin_name: str
    co_op_id: str
    state: str
    proposing_boat: str | None
    version: str | None
    author: str | None
    changelog: str | None
    proposed_at: str | None
    resolved_at: str | None
    reject_reason: str | None
    data_license_gate_passed: bool

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> CatalogEntry:
        return cls(
            plugin_name=row["plugin_name"],
            co_op_id=row["co_op_id"],
            state=row["state"],
            proposing_boat=row.get("proposing_boat"),
            version=row.get("version"),
            author=row.get("author"),
            changelog=row.get("changelog"),
            proposed_at=row.get("proposed_at"),
            resolved_at=row.get("resolved_at"),
            reject_reason=row.get("reject_reason"),
            data_license_gate_passed=bool(row.get("data_license_gate_passed", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_name": self.plugin_name,
            "co_op_id": self.co_op_id,
            "state": self.state,
            "proposing_boat": self.proposing_boat,
            "version": self.version,
            "author": self.author,
            "changelog": self.changelog,
            "proposed_at": self.proposed_at,
            "resolved_at": self.resolved_at,
            "reject_reason": self.reject_reason,
            "data_license_gate_passed": self.data_license_gate_passed,
        }


# ---------------------------------------------------------------------------
# Data licensing gate
# ---------------------------------------------------------------------------


def check_data_license_gate(result: dict[str, Any]) -> list[str]:
    """Validate that no PII-derived fields appear in an AnalysisResult dict.

    Checks metric names and raw data keys against a known list of PII-related
    terms.  Returns a list of offending field identifiers (empty list = pass).

    Per data-licensing.md §8: models promoted to co-op level must not leak
    boat-private data (audio, photos, biometrics, transcripts, notes) in
    their outputs.
    """
    failing: list[str] = []

    for metric in result.get("metrics", []):
        name_lower = str(metric.get("name", "")).lower()
        for term in _PII_TERMS:
            if term in name_lower:
                failing.append(f"metric:{metric.get('name', '')}")
                break

    for key in result.get("raw", {}):
        key_lower = str(key).lower()
        for term in _PII_TERMS:
            if term in key_lower:
                failing.append(f"raw:{key}")
                break

    return failing


# ---------------------------------------------------------------------------
# State machine transitions
# ---------------------------------------------------------------------------


class CatalogError(Exception):
    """Raised when a catalog state transition is invalid."""


async def propose_to_co_op(
    storage: Storage,
    plugin_name: str,
    co_op_id: str,
    *,
    proposing_boat: str,
    version: str,
    author: str = "",
    changelog: str = "",
) -> CatalogEntry:
    """Propose a boat-local plugin for co-op promotion.

    Guard conditions:
    - Plugin name must be unique in the co-op catalog (or previously rejected).
    - The proposing boat must be an active co-op member (caller's responsibility).

    Allowed source states: (none) or rejected.
    """
    existing = await storage.get_catalog_entry(plugin_name, co_op_id)
    if existing is not None and existing["state"] not in (REJECTED,):
        raise CatalogError(
            f"Plugin {plugin_name!r} is already in state {existing['state']!r} for this co-op"
        )

    now = datetime.now(UTC).isoformat()
    await storage.upsert_catalog_entry(
        plugin_name=plugin_name,
        co_op_id=co_op_id,
        state=PROPOSED,
        proposing_boat=proposing_boat,
        version=version,
        author=author,
        changelog=changelog,
        proposed_at=now,
        resolved_at=None,
        reject_reason=None,
        data_license_gate_passed=0,
    )
    logger.info(
        "Plugin {!r} proposed to co-op {!r} by boat {!r}", plugin_name, co_op_id, proposing_boat
    )
    row = await storage.get_catalog_entry(plugin_name, co_op_id)
    assert row is not None
    return CatalogEntry.from_row(row)


async def approve(
    storage: Storage,
    plugin_name: str,
    co_op_id: str,
    *,
    result_sample: dict[str, Any],
) -> CatalogEntry:
    """Approve a proposed plugin (co-op moderator action).

    Runs the data licensing gate before approving.  Raises CatalogError if
    the gate fails or the plugin is not in 'proposed' state.
    """
    existing = await storage.get_catalog_entry(plugin_name, co_op_id)
    if existing is None or existing["state"] != PROPOSED:
        state = existing["state"] if existing else "(not found)"
        raise CatalogError(
            f"Cannot approve plugin {plugin_name!r}: must be in 'proposed' state, got {state!r}"
        )

    # Data licensing gate (EARS: must pass before Proposed → CoopActive)
    failing = check_data_license_gate(result_sample)
    if failing:
        raise CatalogError(
            f"Data license gate failed for {plugin_name!r}; offending fields: {failing}"
        )

    now = datetime.now(UTC).isoformat()
    await storage.upsert_catalog_entry(
        plugin_name=plugin_name,
        co_op_id=co_op_id,
        state=CO_OP_ACTIVE,
        proposing_boat=existing["proposing_boat"],
        version=existing["version"],
        author=existing.get("author"),
        changelog=existing.get("changelog"),
        proposed_at=existing.get("proposed_at"),
        resolved_at=now,
        reject_reason=None,
        data_license_gate_passed=1,
    )
    logger.info("Plugin {!r} approved for co-op {!r}", plugin_name, co_op_id)
    row = await storage.get_catalog_entry(plugin_name, co_op_id)
    assert row is not None
    return CatalogEntry.from_row(row)


async def reject(
    storage: Storage,
    plugin_name: str,
    co_op_id: str,
    *,
    reason: str,
) -> CatalogEntry:
    """Reject a proposed plugin (co-op moderator action)."""
    existing = await storage.get_catalog_entry(plugin_name, co_op_id)
    if existing is None or existing["state"] != PROPOSED:
        state = existing["state"] if existing else "(not found)"
        raise CatalogError(
            f"Cannot reject plugin {plugin_name!r}: must be in 'proposed' state, got {state!r}"
        )

    now = datetime.now(UTC).isoformat()
    await storage.upsert_catalog_entry(
        plugin_name=plugin_name,
        co_op_id=co_op_id,
        state=REJECTED,
        proposing_boat=existing["proposing_boat"],
        version=existing["version"],
        author=existing.get("author"),
        changelog=existing.get("changelog"),
        proposed_at=existing.get("proposed_at"),
        resolved_at=now,
        reject_reason=reason,
        data_license_gate_passed=0,
    )
    logger.info("Plugin {!r} rejected for co-op {!r}: {}", plugin_name, co_op_id, reason)
    row = await storage.get_catalog_entry(plugin_name, co_op_id)
    assert row is not None
    return CatalogEntry.from_row(row)


async def set_co_op_default(
    storage: Storage,
    plugin_name: str,
    co_op_id: str,
) -> CatalogEntry:
    """Set a co_op_active plugin as the co-op default (co-op moderator action).

    Implicitly un-defaults any existing co_op_default plugin.
    """
    existing = await storage.get_catalog_entry(plugin_name, co_op_id)
    if existing is None or existing["state"] != CO_OP_ACTIVE:
        state = existing["state"] if existing else "(not found)"
        raise CatalogError(
            f"Cannot set default for {plugin_name!r}: must be co_op_active, got {state!r}"
        )

    # Clear any existing default in this co-op
    await storage.clear_co_op_default(co_op_id)

    await storage.upsert_catalog_entry(
        plugin_name=plugin_name,
        co_op_id=co_op_id,
        state=CO_OP_DEFAULT,
        proposing_boat=existing["proposing_boat"],
        version=existing["version"],
        author=existing.get("author"),
        changelog=existing.get("changelog"),
        proposed_at=existing.get("proposed_at"),
        resolved_at=existing.get("resolved_at"),
        reject_reason=None,
        data_license_gate_passed=1,
    )
    logger.info("Plugin {!r} set as co-op default for {!r}", plugin_name, co_op_id)
    row = await storage.get_catalog_entry(plugin_name, co_op_id)
    assert row is not None
    return CatalogEntry.from_row(row)


async def unset_co_op_default(
    storage: Storage,
    plugin_name: str,
    co_op_id: str,
) -> CatalogEntry:
    """Revert a co_op_default plugin to co_op_active (co-op moderator action)."""
    existing = await storage.get_catalog_entry(plugin_name, co_op_id)
    if existing is None or existing["state"] != CO_OP_DEFAULT:
        state = existing["state"] if existing else "(not found)"
        raise CatalogError(
            f"Cannot unset default for {plugin_name!r}: must be co_op_default, got {state!r}"
        )

    await storage.upsert_catalog_entry(
        plugin_name=plugin_name,
        co_op_id=co_op_id,
        state=CO_OP_ACTIVE,
        proposing_boat=existing["proposing_boat"],
        version=existing["version"],
        author=existing.get("author"),
        changelog=existing.get("changelog"),
        proposed_at=existing.get("proposed_at"),
        resolved_at=existing.get("resolved_at"),
        reject_reason=None,
        data_license_gate_passed=1,
    )
    logger.info("Plugin {!r} unset as default in co-op {!r}", plugin_name, co_op_id)
    row = await storage.get_catalog_entry(plugin_name, co_op_id)
    assert row is not None
    return CatalogEntry.from_row(row)


async def deprecate(
    storage: Storage,
    plugin_name: str,
    co_op_id: str,
) -> CatalogEntry:
    """Deprecate a co_op_active or co_op_default plugin.

    If the plugin is the co-op default, the default reverts to platform default
    (i.e., the co_op_default state is cleared) before deprecation.
    """
    existing = await storage.get_catalog_entry(plugin_name, co_op_id)
    if existing is None or existing["state"] not in (CO_OP_ACTIVE, CO_OP_DEFAULT):
        state = existing["state"] if existing else "(not found)"
        raise CatalogError(
            f"Cannot deprecate {plugin_name!r}: must be co_op_active or co_op_default,"
            f" got {state!r}"
        )

    now = datetime.now(UTC).isoformat()
    await storage.upsert_catalog_entry(
        plugin_name=plugin_name,
        co_op_id=co_op_id,
        state=DEPRECATED,
        proposing_boat=existing["proposing_boat"],
        version=existing["version"],
        author=existing.get("author"),
        changelog=existing.get("changelog"),
        proposed_at=existing.get("proposed_at"),
        resolved_at=now,
        reject_reason=None,
        data_license_gate_passed=existing.get("data_license_gate_passed", 0),
    )
    logger.info("Plugin {!r} deprecated in co-op {!r}", plugin_name, co_op_id)
    row = await storage.get_catalog_entry(plugin_name, co_op_id)
    assert row is not None
    return CatalogEntry.from_row(row)


async def restore(
    storage: Storage,
    plugin_name: str,
    co_op_id: str,
) -> CatalogEntry:
    """Restore a deprecated plugin to co_op_active (co-op moderator action)."""
    existing = await storage.get_catalog_entry(plugin_name, co_op_id)
    if existing is None or existing["state"] != DEPRECATED:
        state = existing["state"] if existing else "(not found)"
        raise CatalogError(
            f"Cannot restore {plugin_name!r}: must be deprecated, got {state!r}"
        )

    await storage.upsert_catalog_entry(
        plugin_name=plugin_name,
        co_op_id=co_op_id,
        state=CO_OP_ACTIVE,
        proposing_boat=existing["proposing_boat"],
        version=existing["version"],
        author=existing.get("author"),
        changelog=existing.get("changelog"),
        proposed_at=existing.get("proposed_at"),
        resolved_at=existing.get("resolved_at"),
        reject_reason=None,
        data_license_gate_passed=existing.get("data_license_gate_passed", 0),
    )
    logger.info("Plugin {!r} restored in co-op {!r}", plugin_name, co_op_id)
    row = await storage.get_catalog_entry(plugin_name, co_op_id)
    assert row is not None
    return CatalogEntry.from_row(row)
