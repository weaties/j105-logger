---
name: new-migration
description: Add a new SQLite schema migration to storage.py
disable-model-invocation: true
---

# New Schema Migration

The general flow (worktree, TDD, lint) is in CLAUDE.md and `/tdd`. The
schema-migration mechanics are visible in `storage.py` — the
`_CURRENT_VERSION` constant and `_MIGRATIONS` dict show every prior
migration as a worked example. This skill captures only the
migration-specific gotchas:

- **Bump `_CURRENT_VERSION` in `storage.py` by exactly 1** — never skip
  numbers, never reuse one. Version collisions across concurrent
  agent worktrees are the main reason CLAUDE.md mandates worktree
  isolation; check the diff at PR time to confirm no other agent
  shipped the same number while you were branched.

- **Migrations must be idempotent.** Use `CREATE TABLE IF NOT EXISTS`
  and `CREATE INDEX IF NOT EXISTS`. SQLite's `ALTER TABLE` is
  add-column-only — no drops, no renames. Plan accordingly.

- **One migration per logical schema change.** Don't bundle unrelated
  table creations into one version — it makes rollback reasoning and
  the `RELEASES.md` entry harder.

- **Update CLAUDE.md's Stack table** with the new schema version when
  the migration ships (per `/pr-checklist`).

- **Use the `storage` fixture** for tests — it runs all migrations
  on an in-memory database, so a TDD red-step will fail with
  "no such table" until the migration lands. That's the intended
  signal.
