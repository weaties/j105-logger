---
name: skill-compare
description: Blind A/B comparison of two skill versions using eval cases — measures relative improvement across correctness, completeness, conciseness, and actionability
---

# Skill Comparator — Blind A/B Testing

Run the same eval prompts against two versions of a skill and blind-score which
produces better results. Proves that a skill change is an improvement, not just
a change.

## Usage

```
/skill-compare <skill-name>                # compare current branch vs main
/skill-compare <skill-name> --file         # compare SKILL.md vs SKILL.md.new
/skill-compare <skill-name> <case-name>    # compare a single case only
```

## Prerequisites

The target skill must have eval cases in `<skill-dir>/evals/cases.yaml` (see
`/skill-eval` for the case format). If no cases exist, stop with:
"No eval cases found for <skill>. Run `/skill-eval` to add cases first."

## Workflow

### 1. Load the two versions

**Git-based (default):** Compare the current branch's SKILL.md against main's.

```bash
# Version A — main branch
git show main:<relative-path-to-SKILL.md>

# Version B — current working tree
# Read the file directly
```

**File-based (`--file`):** Compare `SKILL.md` against `SKILL.md.new` in the
same skill directory. The `.new` file contains the proposed revision.

Assign each version a random label — either "Alpha" / "Beta" or "Left" / "Right".
**Do not use "old" / "new" or "A" / "B" to avoid biasing the scoring.** Record
the mapping privately (e.g., Alpha = main, Beta = branch) but do not reveal it
until the final report.

If the two versions are identical, stop with:
"Versions are identical — nothing to compare."

### 2. Show the diff

Before running cases, display a concise diff of the two skill versions so the
user can see what changed:

```
### Skill diff: <skill-name>
<unified diff between version Alpha and version Beta>
```

### 3. Run each eval case

For each case in `evals/cases.yaml` (or the single named case):

**a. Generate outputs from both versions:**

For each version, mentally apply that version's SKILL.md instructions to the
case's `scenario` + `mock_input`, and produce the output the skill would
generate. Keep the two outputs separate.

**b. Blind-score on four dimensions:**

Score each output on a 1–5 scale for each dimension, **without knowing which
version produced it** (use the randomized labels from step 1):

| Dimension | What to evaluate | 1 (poor) | 5 (excellent) |
|---|---|---|---|
| **Correctness** | Does the output meet the case's `expected` criteria and avoid `anti_expected`? | Misses critical criteria | Hits all criteria |
| **Completeness** | Are all relevant aspects of the scenario covered? | Major gaps | Thorough coverage |
| **Conciseness** | Is the output appropriately scoped — not bloated, not skeletal? | Padded / rambling OR missing key content | Right-sized for the task |
| **Actionability** | Can the user act on the output without further clarification? | Vague, requires follow-up | Clear next steps, no ambiguity |

**c. Pick a winner for this case:**

For each case, determine: Alpha wins, Beta wins, or Tie. A tie requires scores
within 1 point on all four dimensions. Otherwise, the version with the higher
total score wins the case.

**d. Record the eval-criteria pass/fail too:**

Also check each output against the case's `expected` and `anti_expected`
criteria (as `/skill-eval` does). This lets you see whether the new version
passes more criteria, not just whether it "feels" better.

### 4. Aggregate results

Compute:

- **Case wins:** Alpha X, Beta Y, Ties Z
- **Dimension averages:** mean score per dimension per version
- **Criteria pass rate:** % of eval criteria met per version
- **Overall winner:** version with more case wins (or tie if equal)

### 5. Report results

Output the report in this format:

```
## Skill Compare: <skill-name>
Date: <date>
Versions: Alpha = <source>, Beta = <source>
Cases: <N>

### Summary

| Metric | Alpha | Beta |
|---|---|---|
| Cases won | X | Y |
| Ties | — | Z |
| Avg correctness | 3.8 | 4.2 |
| Avg completeness | 4.0 | 4.1 |
| Avg conciseness | 3.5 | 4.0 |
| Avg actionability | 3.9 | 4.3 |
| Criteria pass rate | 85% | 92% |

**Winner: Beta (branch) — wins X/N cases, +0.4 avg score**
(or: **Tie — neither version is clearly better**)

### Per-case breakdown

| Case | Alpha | Beta | Winner | Notes |
|---|---|---|---|---|
| case-name-1 | 16/20 | 18/20 | Beta | Beta catches PII edge case |
| case-name-2 | 17/20 | 17/20 | Tie | |

### Dimension details

#### Correctness
- Alpha: [per-case scores]
- Beta: [per-case scores]
- Verdict: <which is more correct and why>

#### Completeness
...

#### Conciseness
...

#### Actionability
...

### Criteria diff

Cases where the two versions differ on specific eval criteria:

| Case | Criterion | Alpha | Beta |
|---|---|---|---|
| case-name-1 | "Must flag PII exposure" | FAIL | PASS |
```

### 6. Log results

Append a summary to `.claude/skills/<skill-name>/evals/compare-log.md`:

```markdown
### <date> — <branch-or-file> vs <base>

| Metric | Alpha (<source>) | Beta (<source>) |
|---|---|---|
| Cases won | X | Y |
| Criteria pass rate | 85% | 92% |
| Avg total score | 15.2/20 | 16.6/20 |

Winner: Beta
```

Create the file if it doesn't exist. This log provides a historical record of
how skill refinements have performed over time.

## Scoring Guidelines

### Avoiding bias

- **Randomize label assignment** every run — don't always map main → Alpha
- **Score each output independently** before comparing — don't look at one
  output while scoring the other
- **Anchor to the eval criteria** — the case's `expected` and `anti_expected`
  are the ground truth, not subjective preference
- **When in doubt, tie** — a genuine tie is more honest than a forced winner

### Interpreting results

| Result | What it means | Action |
|---|---|---|
| Beta wins 70%+ cases | Strong improvement — ship it | Merge the skill change |
| Beta wins 50–70% cases | Marginal improvement — review dimensions | Check if conciseness regressed while correctness improved |
| Tie (within 1 case) | No clear winner | The change may not be worth the diff complexity |
| Alpha wins | Regression — the change made things worse | Revert or rethink the approach |

### Dimension trade-offs

Common trade-off patterns to watch for:

- **Correctness up, conciseness down:** Skill got more thorough but also more
  verbose. May be acceptable if the extra content is high-signal.
- **Completeness up, actionability down:** Skill covers more ground but buries
  the key takeaway. Usually a net negative — users need clear next steps.
- **All dimensions flat:** The change is a lateral move. Only worth shipping if
  it improves maintainability of the skill itself.

## Tips

- Start with the priority skills: `/data-license`, `/pr-checklist`, `/spec`,
  `/domain` — these benefit most from measured iteration.
- Run comparisons before and after a skill change, not just after. The "before"
  run establishes whether the eval cases themselves are stable (no flaky scores).
- If a comparison shows a tie but you believe the change is better, the eval
  cases may be too coarse. Add more cases that target the specific improvement.
- Small, focused skill changes are easier to compare than large rewrites.
  If a rewrite touches five aspects of a skill, it's hard to attribute wins to
  any specific change.
