---
name: architecture
description: Codebase comprehension and complexity tracking — module map, data flow, recent changes, and complexity hotspots. Run with no arguments for a full snapshot or with a date/commit/tag for a delta briefing. TRIGGER when the user asks for a system overview, wants to understand the architecture, asks "what changed while I was away", or needs to orient before a large task. DO NOT trigger for specific code questions, bug fixes, or implementation tasks — those are better served by reading the relevant module directly.
---

# Architecture — Codebase Comprehension

Two modes:

- **Full snapshot** (no arguments): Module map, health scores, data flow,
  dependency graph, complexity hotspots, test coverage, risk overlay, debt score.
- **Delta briefing** (`$ARGUMENTS`): What changed since a date/commit/tag —
  risk surface change, test delta, structural shifts, recommendations.

---

## 1. Determine mode

If `$ARGUMENTS` is empty → **full snapshot**. Otherwise parse as a date
(`2026-03-01`, `last week`), commit SHA, or git tag (`stage/2026-03-15`).
Resolve to a commit ref. If ambiguous, ask.

---

## 2. Module map

### Full snapshot

```bash
wc -l src/helmlog/*.py | sort -rn
```

For each module: **Responsibility** (one line), **Key deps** (helmlog imports),
**Risk tier** (from CLAUDE.md). Table grouped by tier (Critical → High →
Standard → Low → Unclassified).

### Delta briefing

Show only added/removed/structurally changed modules since `<ref>`:

```bash
git diff --stat <ref>...HEAD -- src/helmlog/
git log --oneline <ref>...HEAD -- src/helmlog/
```

Flag any module that crossed the 200-line threshold.

---

## 2.5 Module Health Scores

Two signals per module:

**Size grade:** A (<100), B (100-200), C (200-300), D (300-500), F (500+)

**Churn grade** (30-day commits): A (0-2), B (3-5), C (6-10), D (11-20), F (20+)

```bash
git log --since="30 days ago" --format="" --name-only -- src/helmlog/*.py | sort | uniq -c | sort -rn
```

**Tier adjustment:** Critical modules get one grade harsher on both signals.
**Overall** = worst of (adjusted size, adjusted churn).

Only show modules at **C or worse** — healthy modules omitted:

```
| Module | Lines | Size | Churn (30d) | Churn | Tier | Health |
|---|---|---|---|---|---|---|
| storage.py | 5923 | F | 8 | C | Critical | F |
```

If all A/B: "All modules healthy — no scores at C or worse."

---

## 3. Data flow & dependency graph

### Full snapshot

Derive from actual imports:

```bash
grep -rn "^from helmlog\.\|^import helmlog\." src/helmlog/*.py
```

Produce ASCII diagram: (1) instrument ingest, (2) read paths, (3) external
data, (4) federation, (5) audio pipeline.

**Most-depended-on** (top 10 — changes here have biggest blast radius):

```bash
grep -rn "from helmlog\." src/helmlog/*.py | sed 's/.*from helmlog\.\([a-z_]*\).*/\1/' | sort | uniq -c | sort -rn | head -10
```

```
| Module | Imported by N | Tier |
```

**Most-dependent** (top 5 — most affected by changes elsewhere):

```bash
for f in src/helmlog/*.py; do m=$(basename "$f" .py); n=$(grep -c "from helmlog\." "$f" 2>/dev/null || echo 0); echo "$n $m"; done | sort -rn | head -5
```

```
| Module | Imports N | Tier |
```

### Delta briefing

Show only affected data flow paths. Highlight new/removed connections.

---

## 4. Complexity hotspots

### Module size

```bash
wc -l src/helmlog/*.py | sort -rn
```

| Severity | Threshold | Action |
|---|---|---|
| **Watch** | 200-300 lines | May be fine if cohesive |
| **Warning** | 300-500 lines | Review for split opportunities |
| **Alert** | 500+ lines | Strongly recommend splitting |

### Function complexity

For Warning/Alert modules, find high-branching and long (50+ line) functions:

```bash
grep -c "if \|elif \|for \|while \|except \|case " src/helmlog/<module>.py
grep -n "^    def \|^    async def " src/helmlog/<module>.py
```

### Change clustering (delta only)

```bash
git log --format="" --name-only <ref>...HEAD -- src/helmlog/ | sort | uniq -c | sort -rn
```

---

## 4.5 Test Coverage

```bash
uv run pytest --cov=helmlog --cov-report=term-missing --no-header -q 2>/dev/null | tail -20
```

**Flag any Critical/High tier module below 80%:**

```
| Module | Coverage | Tier | Status |
|---|---|---|---|
| federation.py | 45% | Critical | RISK — below 80% |
```

Only table Critical/High modules. Note overall project % for Standard/Low.
If `pytest --cov` fails, skip with a note.

---

## 5. Risk tier overlay

| Tier | Modules |
|---|---|
| **Critical** | `auth.py`, `peer_auth.py`, `federation.py`, `storage.py` (migrations), `can_reader.py` |
| **High** | `sk_reader.py`, `peer_api.py`, `peer_client.py`, `export.py`, `transcribe.py`, `boat_settings.py` |
| **Standard** | `web.py`, `polar.py`, `external.py`, `races.py`, `triggers.py`, `maneuver_detector.py`, `race_classifier.py`, `courses.py` |
| **Low** | Templates, CSS, JS, docs, config, scripts |

**Escalation rule:** Hotspot + Critical/High tier = call out explicitly with
specific split recommendations.

---

## 6. Output format

Target: **Full snapshot** 80-150 lines, **Delta** 30-80 lines.

### Full snapshot structure

```
## Module Map — <table grouped by risk tier>
## Module Health Scores — <C or worse only>
## Data Flow — <ASCII diagram + dependency tables>
## Complexity Hotspots — <module, lines, severity, tier>
## Test Coverage — <Critical/High table + overall %>
## Recommendations — <2-5 bullets>
### Architectural Debt Score
```

**Architectural Debt Score** — single summary combining: modules at Warning/Alert
size + Critical/High modules with health C or worse + coverage gaps (Critical/High
below 80%). Express as:

`Arch debt: N hotspots (X Critical, Y High) — Z coverage gaps`

If clean: `Arch debt: clean`

### Delta briefing structure

```
## Changes Since <ref> — <N commits, M files>
## Risk Surface Change — <new Critical/High code?>
## Test Delta — <test lines added vs source lines added>
## Structural Changes — <new/removed, growth/shrinkage>
## Affected Data Flow — <changed paths>
## Complexity Shifts — <threshold crossings, high churn>
## Recommendations + Architectural Debt Score
```

**Risk Surface Change:** Flag new files, functions, or significant growth in
Critical/High modules.

**Test Delta:** Compute from `git diff --stat <ref>...HEAD` for `tests/` vs
`src/helmlog/`. Flag if ratio < 0.5 (less than one test line per two source
lines). Acceptable: 0.5+. Strong: 1.0+.

---

## 7. What this skill does NOT do

- Does not modify any code
- Does not create issues or PRs
- Does not run tests, lint, or type checks (use `/pr-checklist` for that)
- Does not replace reading the code — it orients you so you know *where* to read
