---
name: release-notes
description: Draft a curated RELEASES.md entry from commits since the last stage/* tag. TRIGGER when preparing to promote main → stage, when the user asks for release notes, or when running the promote workflow. DO NOT trigger for general documentation edits, mid-development work, or changes to RELEASES.md that aren't promotion-related.
---

# /release-notes — Draft a RELEASES.md entry

Format conventions emerge naturally from reading the existing
`RELEASES.md`; this skill encodes only the operational rules that are NOT
recoverable from existing entries.

## Operational rules

- **Commit range:** from the latest `stage/*` tag (by creation date) to
  HEAD. Use `git tag -l 'stage/*' --sort=-creatordate | head -1`. If no
  stage tag exists, use the initial commit.
- **Filter ideation-log-only commits.** A commit whose only changed file
  is `docs/ideation-log.md` is excluded entirely. A commit that touches
  `docs/ideation-log.md` AND other files is included, but the entry
  describes only the non-ideation changes. Use:
  `git diff-tree --no-commit-id --name-only -r <sha>`.
- **If only ideation-log commits remain after filtering**, tell the user:
  "All commits since the last stage tag only touch the ideation log —
  nothing to document. The promote workflow will allow this through
  automatically." Stop there.
- **Today's date** in YYYY-MM-DD goes in the entry heading.
- **Insertion point:** prepend immediately after the `# Release Notes`
  H1 (line 1), separated by a blank line. Do not modify existing entries.
- **Do NOT commit.** Present the draft for review; the user commits when
  satisfied.
- **Do NOT mention the ideation log** anywhere in the entry.

## Link format

Reference numbers as clickable Markdown links. Determine PR vs issue with
`gh pr view <N> --json state -q .state 2>/dev/null` — success means PR.

- PR: `([#NNN](https://github.com/weaties/helmlog/pull/NNN))`
- Issue: `([#NNN](https://github.com/weaties/helmlog/issues/NNN))`

## Theme grouping

Group commits into 2–5 logical themes (e.g., "Performance analysis",
"Synthesizer improvements", "Deploy & infrastructure", "Bug fixes"). One
bullet per logical change — merge related commits into a single bullet.
Use sub-bullets only when a change needs a brief clarification. A
single-commit release can have just one group.
