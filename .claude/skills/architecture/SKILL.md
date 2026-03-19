---
name: architecture
description: Codebase comprehension and complexity tracking — module map, data flow, recent changes, and complexity hotspots. Run with no arguments for a full snapshot or with a date/commit/tag for a delta briefing. TRIGGER when the user asks for a system overview, wants to understand the architecture, asks "what changed while I was away", or needs to orient before a large task. DO NOT trigger for specific code questions, bug fixes, or implementation tasks — those are better served by reading the relevant module directly.
---

# Architecture — Codebase Comprehension

Produce a concise, scannable system overview. Two modes:

- **Full snapshot** (no arguments): Complete system overview — module map, data
  flow, complexity hotspots, risk tier overlay. Use after returning from a break.
- **Delta briefing** (`$ARGUMENTS`): What changed since a date, commit, or tag.
  Shows structural changes, new/removed modules, complexity shifts, and affected
  data flow paths.

---

## 1. Determine mode

If `$ARGUMENTS` is empty → **full snapshot**.

Otherwise parse `$ARGUMENTS` as one of:
- A date (`2026-03-01`, `last week`, `yesterday`)
- A commit SHA or short SHA
- A git tag (`stage/2026-03-15`)

Resolve to a commit ref for `git log` and `git diff`. If ambiguous, ask.

---

## 2. Module map

### Full snapshot

List every `.py` module under `src/helmlog/` with:

```bash
wc -l src/helmlog/*.py | sort -rn
```

For each module, provide:
- **Responsibility** (one line — derive from docstring, class names, or function names)
- **Key dependencies** (imports from other helmlog modules)
- **Risk tier** (from the Risk Tiers table in CLAUDE.md; "Unclassified" if missing)

Format as a table. Group by risk tier (Critical → High → Standard → Low → Unclassified).

### Delta briefing

Show only modules that were added, removed, or structurally changed (new
classes, new public functions, significant line count changes) since the
reference commit:

```bash
git diff --stat <ref>...HEAD -- src/helmlog/
git log --oneline <ref>...HEAD -- src/helmlog/
```

Flag any module that crossed the 200-line threshold in either direction.

---

## 3. Data flow

### Full snapshot

Trace the primary data flow paths through the codebase. Derive these from
actual imports, not from memory. Check the current import graph:

```bash
grep -rn "^from helmlog\.\|^import helmlog\." src/helmlog/*.py
```

Produce a concise ASCII diagram showing:
1. **Instrument ingest:** Signal K / CAN → decoded records → storage
2. **Read paths:** storage → web, export, polar, maneuver detection
3. **External data:** weather, tides → storage
4. **Federation:** peer_client ↔ peer_api, federation, peer_auth
5. **Audio pipeline:** audio → transcribe → storage → web

### Delta briefing

Show only data flow paths that were affected by changes since the reference.
Highlight new connections and removed connections.

---

## 4. Complexity hotspots

Identify modules and functions that may need attention.

### Module size

```bash
wc -l src/helmlog/*.py | sort -rn
```

Flag any module exceeding 200 lines (the project convention from CLAUDE.md).
For modules well over 200 lines, note how far over they are:

| Severity | Threshold | Action |
|---|---|---|
| **Watch** | 200-300 lines | Note — may be fine if cohesive |
| **Warning** | 300-500 lines | Recommend reviewing for split opportunities |
| **Alert** | 500+ lines | Strongly recommend splitting |

### Function complexity

For modules at Warning or Alert level, identify functions with high branching
complexity:

```bash
grep -c "if \|elif \|for \|while \|except \|case " src/helmlog/<module>.py
```

Also scan for long functions (rough heuristic — functions spanning many lines):

```bash
grep -n "^    def \|^    async def " src/helmlog/<module>.py
```

Flag functions that appear to span more than 50 lines (estimate from line
number gaps between consecutive `def` lines).

### Change clustering (delta mode only)

For delta briefings, identify files with disproportionate churn:

```bash
git log --format="" --name-only <ref>...HEAD -- src/helmlog/ | sort | uniq -c | sort -rn
```

Files appearing in many commits may be complexity magnets.

---

## 5. Risk tier overlay

Cross-reference hotspots with the Risk Tiers table from CLAUDE.md:

| Tier | Modules |
|---|---|
| **Critical** | `auth.py`, `peer_auth.py`, `federation.py`, `storage.py` (migrations), `can_reader.py` |
| **High** | `sk_reader.py`, `peer_api.py`, `peer_client.py`, `export.py`, `transcribe.py`, `boat_settings.py` |
| **Standard** | `web.py`, `polar.py`, `external.py`, `races.py`, `triggers.py`, `maneuver_detector.py`, `race_classifier.py`, `courses.py` |
| **Low** | Templates, CSS, JS, docs, config, scripts |

**Escalation rule:** A complexity hotspot in a Critical or High tier module is
more urgent than one in a Standard module. Call these out explicitly:

> **web.py** (Standard, 6868 lines, Alert) — massively exceeds convention but
> has E501 suppression; splitting would require route-group extraction.
>
> **storage.py** (Critical when migrations touched, 5923 lines, Alert) — schema
> migrations + query methods in one file; migration extraction would reduce risk.

---

## 6. Output format

Structure the output as a briefing, not a dump. Use headers, tables, and short
prose. Target length:

- **Full snapshot:** 80-150 lines of output
- **Delta briefing:** 30-80 lines of output

### Full snapshot structure

```
## Module Map
<table grouped by risk tier>

## Data Flow
<ASCII diagram>

## Complexity Hotspots
<table: module, lines, severity, tier, notes>

## Recommendations
<2-5 bullet points: most actionable observations>
```

### Delta briefing structure

```
## Changes Since <ref> (<date>)
<N commits, M files changed>

## Structural Changes
<new/removed modules, significant growth/shrinkage>

## Affected Data Flow
<which paths changed>

## Complexity Shifts
<modules that crossed thresholds or had high churn>

## Recommendations
<2-5 bullet points>
```

---

## 7. What this skill does NOT do

- Does not modify any code
- Does not create issues or PRs
- Does not run tests, lint, or type checks (use `/pr-checklist` for that)
- Does not replace reading the code — it orients you so you know *where* to read
