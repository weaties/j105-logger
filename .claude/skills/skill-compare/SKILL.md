---
name: skill-compare
description: Blind A/B comparison of two skill versions using eval cases — measures relative improvement across correctness, completeness, conciseness, and actionability.
---

# /skill-compare — Blind A/B testing

Run the same eval prompts against two skill versions and blind-score
which produces better results. Proves a skill change is an improvement,
not just a change.

## Usage

```
/skill-compare <skill-name>                # current branch vs main
/skill-compare <skill-name> --file         # SKILL.md vs SKILL.md.new
/skill-compare <skill-name> <case-name>    # single named case only
```

If the target has no `evals/cases.yaml`, stop with: "No eval cases
found for <skill>. Run `/skill-eval` to add cases first."

## Workflow

### 1. Load the two versions

- Default: `git show main:<path-to-SKILL.md>` vs working-tree file.
- `--file`: `SKILL.md` vs `SKILL.md.new` in the same directory.

Assign random labels — **Alpha / Beta** or **Left / Right**, never
"old/new" or "A/B" (those bias scoring). Record the mapping privately;
reveal at the end. If the two versions are identical, stop.

### 2. Show the diff

Print a unified diff of the two SKILL.md versions before running cases,
so the user sees what changed.

### 3. For each eval case

a. **Generate outputs from both versions.** Mentally apply each
   version's instructions to the case's `scenario` + `mock_input`.
   Keep the two outputs separate.

b. **Blind-score on four dimensions** (1–5 each, scored without knowing
   which version produced the output):

   | Dimension | What to evaluate |
   |---|---|
   | Correctness | Hits `expected`, avoids `anti_expected` |
   | Completeness | All relevant aspects of the scenario covered |
   | Conciseness | Right-sized — neither bloated nor skeletal |
   | Actionability | User can act without further clarification |

c. **Pick a per-case winner.** Total scores within 1 point on all four
   dimensions = Tie. Otherwise higher total wins.

d. **Also record `expected` / `anti_expected` pass/fail** as
   `/skill-eval` does — distinguishes "feels better" from "passes
   more criteria."

### 4. Aggregate and report

Summary table with cases won, ties, dimension averages, criteria pass
rate. Per-case breakdown table. Reveal label mapping and declare a
winner (or tie). Append the summary to
`<skill-dir>/evals/compare-log.md` as a historical record.

### 5. Interpreting results

| Result | Action |
|---|---|
| Beta wins ≥70% of cases | Strong improvement — ship it |
| Beta wins 50–70% | Marginal — check for trade-offs (e.g., correctness up but conciseness down) |
| Tie within 1 case | Not worth the diff complexity |
| Alpha wins | Regression — revert or rethink |

## Bias guards

- **Randomize label assignment every run.** Don't always map main → Alpha.
- **Score each output independently** before comparing — don't look at
  one output while scoring the other.
- **When in doubt, tie.** A genuine tie is more honest than a forced
  winner.
- If a comparison is a tie but you believe the change is better, the
  eval cases may be too coarse — add cases that target the specific
  improvement.
