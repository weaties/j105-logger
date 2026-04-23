# Risk Tiers

Not all code carries the same blast radius. Verification effort should be
proportional to risk. The `/pr-checklist` skill resolves the tier automatically
from changed files and adjusts its checks accordingly.

| Tier | Modules | Blast radius | Verification |
|---|---|---|---|
| **Critical** | `auth.py`, `peer_auth.py`, `federation.py`, `storage.py` (migrations), `can_reader.py` | Data loss, security (auth bypass), safety (bad data on displays during racing), data corruption | TDD + integration tests + `/data-license` review + spec review before implementation |
| **High** | `sk_reader.py`, `peer_api.py`, `peer_client.py`, `export.py`, `transcribe.py`, `boat_settings.py` | Incorrect data capture, broken federation, PII exposure | TDD + integration tests where applicable |
| **Standard** | `web.py`, `polar.py`, `external.py`, `races.py`, `triggers.py`, `maneuver_detector.py`, `race_classifier.py`, `courses.py` | Wrong numbers on screen, broken features | TDD + standard PR checklist |
| **Low** | Templates, CSS, JS, docs, config, scripts | Visual issues, non-functional | Smoke test / visual check |

## Rules

- A PR's tier is the **highest** tier of any file it touches.
- Tier assignments are updated when modules change scope (e.g., if
  `can_reader.py` gains CAN write capability, it stays Critical).
- New modules default to **Standard** until explicitly classified.

## Structured specs

Critical and High tier features (and any feature with combinatorial
role × policy × state logic, lifecycle state machines, or hardware-critical
behavior) should have a structured spec written via `/spec` before TDD begins.

| Format | When to use | Example |
|---|---|---|
| **Decision table** | Policy/permission logic with multiple inputs | Thread visibility: role × tier × co-op policy → allowed/denied |
| **State diagram** | Lifecycle with named states and transitions | Plugin lifecycle: available → selected → active → deprecated |
| **EARS requirements** | Hardware/safety-critical behavior with conditions | WHEN polar confidence < 0.5 THE SYSTEM SHALL stop publishing |

Workflow: spec → human reviews spec (posted as issue comment) → TDD from
spec → implement → PR.
