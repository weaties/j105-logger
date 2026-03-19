"""Pluggable analysis framework (#283, #285).

Re-exports the public API for convenience.
"""

from helmlog.analysis.cache import AnalysisCache
from helmlog.analysis.catalog import (
    ACTIVE_STATES,
    ALL_STATES,
    BOAT_LOCAL,
    CO_OP_ACTIVE,
    CO_OP_DEFAULT,
    DEPRECATED,
    PROPOSED,
    REJECTED,
    CatalogEntry,
    CatalogError,
    approve,
    check_data_license_gate,
    deprecate,
    propose_to_co_op,
    reject,
    restore,
    set_co_op_default,
    unset_co_op_default,
)
from helmlog.analysis.discovery import discover_plugins, get_plugin, load_session_data
from helmlog.analysis.preferences import resolve_preference, set_preference
from helmlog.analysis.protocol import (
    AnalysisContext,
    AnalysisPlugin,
    AnalysisResult,
    Insight,
    Metric,
    PluginMeta,
    SessionData,
    VizData,
)

__all__ = [
    "ACTIVE_STATES",
    "ALL_STATES",
    "BOAT_LOCAL",
    "CO_OP_ACTIVE",
    "CO_OP_DEFAULT",
    "DEPRECATED",
    "PROPOSED",
    "REJECTED",
    "AnalysisCache",
    "AnalysisContext",
    "AnalysisPlugin",
    "AnalysisResult",
    "CatalogEntry",
    "CatalogError",
    "Insight",
    "Metric",
    "PluginMeta",
    "SessionData",
    "VizData",
    "approve",
    "check_data_license_gate",
    "deprecate",
    "discover_plugins",
    "get_plugin",
    "load_session_data",
    "propose_to_co_op",
    "reject",
    "resolve_preference",
    "restore",
    "set_co_op_default",
    "set_preference",
    "unset_co_op_default",
]
