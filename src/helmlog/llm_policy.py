"""Policy gates for LLM transcript Q&A and callback detection (#697 spec §3).

Two gates run before any LLM call:

1. **Consent gate** — diarized transcripts are PII routed to a hosted LLM.
   An admin must acknowledge the data flow once before any query is allowed.
2. **Cost-cap state machine** — per-race aggregate spend determines whether
   a query goes through cleanly (UnderSoft), needs a UI confirmation
   (SoftWarned), or is blocked (AtCap). The pre-flight estimate guards
   against a single query pushing spend past the hard cap mid-call.

Both gates collapse into a single ``check_can_query`` call returning a
``PolicyCheck`` that the route handler uses to decide HTTP response shape.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from helmlog.storage import Storage

# First-pass defaults (spec open question 2). Per-race admin override via
# llm_race_caps takes precedence.
LLM_SOFT_WARN_USD_DEFAULT: float = 1.00
LLM_HARD_CAP_USD_DEFAULT: float = 5.00


class CostCapState(enum.Enum):
    UNDER_SOFT = "UnderSoft"
    SOFT_WARNED = "SoftWarned"
    AT_CAP = "AtCap"


@dataclass(frozen=True)
class EffectiveCaps:
    soft_warn_usd: float
    hard_cap_usd: float


@dataclass(frozen=True)
class PolicyCheck:
    allowed: bool
    state: CostCapState
    requires_confirmation: bool
    reason: str | None
    current_spend_usd: float
    soft_warn_usd: float
    hard_cap_usd: float


async def get_effective_caps(storage: Storage, race_id: int) -> EffectiveCaps:
    override = await storage.get_race_caps(race_id)
    if override:
        return EffectiveCaps(
            soft_warn_usd=float(
                override["soft_warn_usd"]
                if override["soft_warn_usd"] is not None
                else LLM_SOFT_WARN_USD_DEFAULT
            ),
            hard_cap_usd=float(
                override["hard_cap_usd"]
                if override["hard_cap_usd"] is not None
                else LLM_HARD_CAP_USD_DEFAULT
            ),
        )
    return EffectiveCaps(LLM_SOFT_WARN_USD_DEFAULT, LLM_HARD_CAP_USD_DEFAULT)


def _classify(spend_usd: float, caps: EffectiveCaps) -> CostCapState:
    if spend_usd >= caps.hard_cap_usd:
        return CostCapState.AT_CAP
    if spend_usd >= caps.soft_warn_usd:
        return CostCapState.SOFT_WARNED
    return CostCapState.UNDER_SOFT


async def check_can_query(
    storage: Storage,
    race_id: int,
    *,
    estimate_usd: float,
) -> PolicyCheck:
    """Single-call gate for Q&A and callback-detection routes."""
    caps = await get_effective_caps(storage, race_id)
    spend = await storage.race_llm_cost(race_id)
    state = _classify(spend, caps)

    consent = await storage.get_llm_consent()
    if consent is None:
        return PolicyCheck(
            allowed=False,
            state=state,
            requires_confirmation=False,
            reason="consent_required",
            current_spend_usd=spend,
            soft_warn_usd=caps.soft_warn_usd,
            hard_cap_usd=caps.hard_cap_usd,
        )

    if state is CostCapState.AT_CAP:
        return PolicyCheck(
            allowed=False,
            state=state,
            requires_confirmation=False,
            reason="hard_cap_reached",
            current_spend_usd=spend,
            soft_warn_usd=caps.soft_warn_usd,
            hard_cap_usd=caps.hard_cap_usd,
        )

    if spend + estimate_usd >= caps.hard_cap_usd:
        return PolicyCheck(
            allowed=False,
            state=CostCapState.AT_CAP,
            requires_confirmation=False,
            reason="would_exceed_cap",
            current_spend_usd=spend,
            soft_warn_usd=caps.soft_warn_usd,
            hard_cap_usd=caps.hard_cap_usd,
        )

    return PolicyCheck(
        allowed=True,
        state=state,
        requires_confirmation=state is CostCapState.SOFT_WARNED,
        reason=None,
        current_spend_usd=spend,
        soft_warn_usd=caps.soft_warn_usd,
        hard_cap_usd=caps.hard_cap_usd,
    )
