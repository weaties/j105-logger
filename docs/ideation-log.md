# Ideation Log

Half-baked ideas that aren't yet actionable enough for GitHub issues. Each entry
captures early thinking so it isn't lost. When an idea matures, it gets promoted
to one or more GitHub issues and its status changes to `promoted`.

## Statuses

| Status | Meaning |
|---|---|
| `raw` | Just captured, no validation or design work |
| `evolving` | Being discussed or refined across conversations |
| `superseded` | Replaced by a different approach (link the replacement) |
| `promoted` | Converted to GitHub issue(s) (link the issue numbers) |

---

## IDX-001: Cross-co-op discussion threads

- **Date captured:** 2026-03-13
- **Origin:** Discussion about threaded comments feature
- **Status:** `raw`
- **Related:** `docs/data-licensing.md`, `docs/federation-design.md`, threaded comments feature

**Description:**
Discussion threads that span across co-ops (not just within a single co-op).
Would allow boats in different co-ops to have shared conversations. Deferred as
a separate phase/feature because it has data-licensing implications — co-op data
boundaries would need to be addressed. May require amendments to
data-licensing.md and federation-design.md.

**Notes:**
- *2026-03-13:* Initial capture. Data-licensing implications are the main blocker
  — cross-co-op threads would need to reconcile different co-ops' data policies.

---

## IDX-002: Scalable plugin distribution

- **Date captured:** 2026-03-13
- **Origin:** Discussion about pluggable analysis/visualization
- **Status:** `raw`
- **Related:** analysis/visualization plugin system

**Description:**
Current plugin model (Python classes as PRs to the repo) works for early
adoption (a few boats, one co-op). When the platform grows to dozens of co-ops,
hundreds of boats, and multiple developers, will need a more scalable
distribution mechanism — possibly a package registry, marketplace, or separate
plugin repos. Don't solve now, but don't block evolution toward it.

**Notes:**
- *2026-03-13:* Initial capture. Current PR-based model is fine for now. Watch
  for signs that it's becoming a bottleneck.

---

## IDX-003: Notification channel expansion (SMS, WhatsApp, push)

- **Date captured:** 2026-03-13
- **Origin:** Discussion about comment notifications
- **Status:** `raw`
- **Related:** threaded comments feature, notification preferences

**Description:**
Platform launches with in-app indicators and email notifications. Future
channels include SMS (Twilio), WhatsApp (Business API), mobile push
notifications. Notification system is designed as channel-pluggable so
contributors can add channels without architectural changes. Each channel has
cost/complexity implications.

**Notes:**
- *2026-03-13:* Initial capture. Email + in-app is sufficient for launch. SMS
  and push are the most likely next channels.

---

## IDX-004: Custom JS visualization plugins

- **Date captured:** 2026-03-13
- **Origin:** Discussion about visualization architecture
- **Status:** `raw`
- **Related:** visualization plugin system

**Description:**
The baseline visualization plugin model uses Python-defined Plotly JSON specs.
For advanced use cases (3D boat models, custom canvas animations, novel
interactive widgets), a `CustomJSVisualization` plugin type could allow loading
custom JS bundles. Not needed early — the Plotly model covers sailing analysis
needs — but the plugin registry should not preclude this evolution.

**Notes:**
- *2026-03-13:* Initial capture. The plugin base class design should leave room
  for a JS subclass without requiring it now.

---

## IDX-005: Tuning guide auto-population from wind range

- **Date captured:** 2026-03-13
- **Origin:** Previous conversation about boat settings capture (referenced in memory)
- **Status:** `raw`
- **Related:** boat settings capture (#274, #275, #276), `src/helmlog/polar.py`

**Description:**
Pre-populate boat tuning settings (shroud tensions, halyard positions, etc.)
based on wind range using the boat's tuning guide. Would connect the boat
settings capture feature with polar/performance data. Identified as a separate
future feature during the boat settings design.

**Notes:**
- *2026-03-13:* Initial capture. Depends on boat settings capture being
  implemented first. Could leverage polar data to suggest settings for conditions.

---

## IDX-006: HelmLog platform-level discussion (GitHub Discussions)

- **Date captured:** 2026-03-13
- **Origin:** Discussion about threaded comments feature
- **Status:** `raw`
- **Related:** threaded comments feature, GitHub repo

**Description:**
Platform-level discussion (not boat or co-op level) should live in the GitHub
Discussions repo rather than building custom infrastructure. This is for platform
community conversations, feature requests, etc. — distinct from the in-app race
discussion threads.

**Notes:**
- *2026-03-13:* Initial capture. GitHub Discussions is zero-cost and already
  integrated with the development workflow.
