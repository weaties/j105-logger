---
name: diagnose
description: Systematic Pi troubleshooting runbook — checks all subsystems (systemd, nginx, Signal K, SQLite, audio, InfluxDB, Tailscale) and reports health. TRIGGER when the user reports a Pi problem ("helmlog is down", "not recording", "web interface broken", "service won't start") or asks for a health check. DO NOT trigger for development issues on Mac, test failures, code questions, or deployment instructions (use /deploy-pi for those).
---

# /diagnose — Pi Troubleshooting Runbook

Produce a structured diagnostic plan when the Pi misbehaves. The actual
commands and known-failure signatures live in the code (`scripts/setup.sh`
defines the units; `can_reader.py`, `sk_reader.py`, `audio.py`, `storage.py`
each document their own failure modes); read those rather than duplicating
them here. This skill specifies the **runbook shape** — phases, status
tags, dependency rules, summary format.

## Usage

- `/diagnose` — run all subsystem checks
- `/diagnose <subsystem>` — one of: `system`, `services`, `can`, `signalk`,
  `audio`, `database`, `network`, `aihat`. Argument is `$ARGUMENTS`.

## Status tags (use these exact strings)

```
[OK]   <check>  — <detail>
[WARN] <check>  — <detail> → <suggested fix>
[FAIL] <check>  — <detail> → <suggested fix>
[SKIP] <check>  — skipped because <dependency> is <state>
```

Every check produces exactly one status line. Group commands under the
check, but the status line is what the operator scans.

## Dependency graph (skip rules)

```
System Health
  └── Services         (skip if filesystem read-only)
        ├── CAN Bus    (skip if helmlog service down — runs in-process)
        ├── Signal K   (skip if signalk service down)
        ├── Audio      (skip if helmlog service down — runs in-process)
        └── AI HAT     (skip if helmlog service down — runs in-process)
Database               (independent — always run)
Network                (independent — always run)
```

**Key fact:** CAN ingest, Signal K subscription, audio capture, and the
FastAPI app all live inside the `helmlog` process. If `helmlog` is
inactive, those checks have no consumer to observe — mark `[SKIP]` with
that reason, do not attempt them.

If a parent check fails, emit `[SKIP]` for every dependent and continue;
never silently omit a subsystem.

## Phasing (when helmlog is down)

When the request is a full diagnosis and `helmlog` is inactive, run in
three phases — do not interleave:

1. **Independent checks first** — system health, database, network. These
   tell you whether the host is healthy enough to run the app at all.
2. **Inspect why helmlog is inactive, then restart** — `systemctl status`,
   `journalctl -u helmlog -n 80`, match against known failure signatures
   (most common: `ModuleNotFoundError` from a stale venv after `git pull`
   — the service runs `--no-sync`). Restart and confirm `is-active`.
3. **Dependent checks only after helmlog is active.** Until then, every
   in-process subsystem is `[SKIP]`.

For partial outages (helmlog active but a specific subsystem failing),
phasing is unnecessary — just walk the dependency graph.

## Summary format (always emit at the end)

```
=== /diagnose summary ===
Total checks:  N
[OK]   N
[WARN] N
[FAIL] N
[SKIP] N

Most likely root cause: <one sentence, derived from the failure pattern>
Recommended next action: <one command or one decision>
```

If there are no failures, omit the root-cause line and emit
`No failures — system is healthy`.

## Out of scope

- Does not modify code, configs, or services.
- Does not run lint/tests/type checks.
- Does not deploy. Use `/deploy-pi` for that.
- AI HAT is not yet deployed; `[OK] AI HAT — not installed (expected)` is
  the expected outcome until that changes.
