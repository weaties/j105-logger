---
name: skill-eval
description: Run evaluation test cases against a skill to measure quality, detect regressions, and benchmark performance
---

# Skill Evaluation Runner

Run test cases against a skill to measure whether it produces correct, consistent
output. Use this to establish baselines, detect regressions after model or skill
changes, and compare skill versions.

## Usage

```
/skill-eval <skill-name>           # run all evals for a skill
/skill-eval <skill-name> <case>    # run a single named case
/skill-eval all                    # run evals for all skills that have them
```

## How It Works

Each skill can have test cases in `<skill-dir>/evals/cases.yaml`. Each test case
defines a scenario, the input the skill would receive, and criteria for judging
the output.

The eval runner:
1. Reads the target skill's SKILL.md (the instructions being tested)
2. Reads the test cases from `evals/cases.yaml`
3. For each case, applies the skill's instructions to the test scenario
4. Scores the output against the case's pass/fail criteria
5. Reports results with a summary scorecard

## Workflow

### 1. Load skill and cases

Read the target skill's `SKILL.md` and its `evals/cases.yaml`. If no cases
exist, report "No eval cases found for <skill>" and stop.

### 2. For each test case

**a. Set up the scenario:**
Read the case's `scenario` field, which describes the context the skill would
operate in (e.g., a code diff, an issue body, a set of changed files). The
`mock_input` field provides the specific input the skill would process.

**b. Apply the skill:**
Following the target skill's instructions exactly as written in its SKILL.md,
produce the output the skill would generate for this scenario. Do not use
knowledge beyond what the skill's instructions and the test scenario provide.

**c. Score against criteria:**
Check the output against each criterion in `expected` (things that MUST be
present) and `anti_expected` (things that must NOT be present). Score each
criterion as pass or fail.

**d. Record the result:**
For each case: case name, pass/fail, which criteria passed, which failed,
and a brief note on any failures.

### 3. Report results

Output a scorecard in this format:

```
## Skill Eval: <skill-name>
Date: <date>

| Case | Result | Pass | Fail | Notes |
|---|---|---|---|---|
| case-name-1 | PASS | 4/4 | 0 | |
| case-name-2 | FAIL | 2/3 | 1 | missed: must flag PII exposure |

**Overall: 5/7 criteria passed (71%)**

### Failures
- case-name-2 / criterion "must flag PII exposure":
  Output did not mention PII or personal data in the context of crew emails.
```

A case passes only if ALL its `expected` criteria pass and NONE of its
`anti_expected` criteria trigger.

### 4. Benchmark mode

When running all cases for a skill, also report:
- **Pass rate:** percentage of cases that fully passed
- **Criteria hit rate:** percentage of individual criteria met across all cases
- **Consistency notes:** any cases where the result seems borderline or
  model-dependent (flag these for potential flakiness)

## Test Case Format

Cases live in `<skill-dir>/evals/cases.yaml`:

```yaml
- name: descriptive-kebab-case-name
  description: What this test verifies (one line)
  type: capability | preference | trigger
  scenario: |
    Multi-line description of the setup context.
    What files exist, what the project state is, what just happened.
  mock_input: |
    The specific input the skill would process — a diff, an issue body,
    a user prompt, etc. This is what the skill "sees."
  expected:
    - criterion: "Description of what the output MUST contain or do"
      weight: critical | important | nice-to-have
    - criterion: "Another required element"
      weight: critical
  anti_expected:
    - criterion: "Description of what the output must NOT contain or do"
      weight: critical
  tags: [optional, categorization, tags]
```

### Weight definitions

| Weight | Meaning | Scoring |
|---|---|---|
| `critical` | Missing this means the skill fundamentally failed | Case fails |
| `important` | Should be present for a good result | Noted in report |
| `nice-to-have` | Would improve the result but not essential | Noted if missing |

A case **fails** if any `critical` criterion is missed or any `critical`
anti-criterion triggers. Non-critical misses are reported but don't fail the case.

### Test case types

| Type | What it tests | Example |
|---|---|---|
| `capability` | Can the skill produce correct output for this scenario? | Does `/data-license` catch a PII leak? |
| `preference` | Does the skill follow the encoded workflow correctly? | Does `/tdd` write the test before the implementation? |
| `trigger` | Would the skill correctly activate (or not) for this prompt? | Does `/pr-checklist` trigger before PR creation? |

## Adding Evals to a New Skill

When creating a new skill, add at least 3 eval cases:
1. One **happy path** — a clear-cut scenario where the skill should succeed
2. One **edge case** — a tricky scenario that tests the skill's boundaries
3. One **negative case** — a scenario where the skill should correctly decline
   or produce a "no action needed" result

## Tips

- Test cases should be **self-contained** — all context needed is in the
  `scenario` and `mock_input` fields. Don't rely on actual repo state.
- Write cases that test the **skill's instructions**, not general model
  capability. If a case would pass without the skill loaded, it's not testing
  the skill.
- For capability uplift skills (`/data-license`, `/spec`), focus cases on
  scenarios where the base model would likely get it wrong without the skill's
  specific guidance.
- Review flaky cases — if a case sometimes passes and sometimes fails, the
  criterion may be too vague or the scenario too ambiguous.
- When a real-world skill failure occurs, capture it as a new eval case to
  prevent regression.
