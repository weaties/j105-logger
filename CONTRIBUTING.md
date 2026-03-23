# Contributing to Helm Log

## Getting Started

```bash
brew install portaudio libsndfile   # macOS deps
uv sync                             # install Python deps
cp .env.example .env                # configure local settings
```

## Development Workflow

### Issue → PR lifecycle

1. **Claim the issue**: apply the `in-progress` label and comment:
   ```bash
   gh issue edit <number> --add-label "in-progress"
   gh issue comment <number> --body "In progress on \`<branch-name>\`"
   ```
2. Branch off `main`: `git checkout -b feature/my-feature main`
3. Follow TDD (`/tdd` skill): write a failing test, implement, pass, lint
4. For complex features touching Critical/High tier modules, write a
   structured spec first (`/spec` skill) and post it on the issue for review
5. Run all checks before pushing (see below)
6. Push and create a PR targeting `main`
7. PR body **must** include `Closes #<issue>` (or `Fixes #<issue>` for bugs)
   so GitHub auto-closes the issue on merge
8. All changes to `main` must come through merged PRs — never push directly

### Required Checks

```bash
uv run pytest                    # all tests pass
uv run ruff check .              # lint clean
uv run ruff format --check .     # format clean
uv run mypy src/                 # types clean
```

For federation/co-op/peer API changes, also run integration tests:

```bash
uv run pytest tests/integration/ -v
```

### Promotion gate

The `promote.yml` workflow gates `main → stage` promotion on RELEASES.md:
a new `##` heading must exist in the promoted commits. Use `/release-notes`
to draft the entry before promoting.

## Data Licensing Policy

Helm Log includes a **data licensing policy** (`docs/data-licensing.md`) that
governs how sailing data is owned, shared, and protected. All contributors
must understand and respect this policy when writing code that handles data.

### Key Principles

- **Boat owners own their data.** Code must never prevent a boat from
  exporting its own data in open formats (CSV, GPX, JSON)
- **PII is protected.** Audio, photos, email addresses, and biometric data
  are personally identifiable information with deletion and anonymization
  rights. Code handling PII must support these rights
- **Co-op data is view-only.** API endpoints serving co-op data from other
  boats must not support bulk export. Rate limiting and audit logging are
  required
- **Gambling prohibition.** No API or feature may facilitate the use of co-op
  data for betting or wagering purposes
- **Protest firewall.** Co-op data from other boats must not be exportable in
  formats designed for submission to protest committees
- **Biometric data is separate.** If adding wearable sensor support, biometric
  data requires per-person consent independent of instrument data sharing
- **Temporal sharing.** Session data may be embargoed per co-op policy. Code
  must respect embargo timestamps before serving track data

### When the Policy Applies

Any code change that touches these areas must be checked against
`docs/data-licensing.md`:

- Data storage, export, or deletion
- API endpoints that serve session or instrument data
- Authentication, access control, or user management
- Audio recording, transcription, or diarization
- Co-op membership, sharing, or governance features
- Any new data type collection (photos, biometrics, etc.)

Use the `/data-license` skill to review changes against the policy.

## Coding Standards

- **Python 3.12+** with full type annotations
- **Ruff** for linting and formatting (line length 100)
- **Loguru** for logging (never `print()`)
- **Dataclasses or TypedDict** for structured data
- Modules stay under ~200 lines; split when they grow
- Hardware-dependent code is isolated (see `CLAUDE.md`)

## Testing

- Tests live in `tests/` and run without hardware (~1,100+ test functions)
- Use the `storage` fixture from `conftest.py` for in-memory SQLite
- Use `httpx.AsyncClient` with `ASGITransport` for web route tests
- Mock hardware modules (`audio.py`, `can_reader.py`, `sk_reader.py`, `cameras.py`)

### Integration tests

Federation, co-op, and peer API changes require integration tests (`tests/integration/`).
Three layers are available:

| Layer | What | When |
|---|---|---|
| **1 — In-process pytest** | Two boats with real Ed25519 keys, in-memory SQLite, `httpx.ASGITransport` | Every PR touching federation code (runs in CI) |
| **2 — Pi harness** | Mac orchestrator → two real Pis over Tailscale | Pre-merge manual validation for federation PRs |
| **3 — Docker compose** | Two containers on Mac (arm64 capable) | Network failure / process isolation testing |

Use the `/integration-test` skill to run the appropriate layer.

## AI Agent Collaboration

HelmLog is developed with AI coding agents (primarily Claude Code). If you use
AI agents in your contributions:

- **Co-Authored-By**: Include `Co-Authored-By: <Agent> <noreply@anthropic.com>`
  in commit messages when an AI agent wrote or substantially modified the code
- **TDD workflow**: Agents should follow the same TDD cycle as human
  contributors — write a failing test first, then implement
- **Human review required**: Schema migrations, auth changes, data deletion
  logic, and anything touching `storage.py` FK constraints or `auth.py` must
  have human review regardless of who (or what) wrote the code
- **Agent-friendly issues**: Well-scoped issues with clear acceptance criteria,
  test cases, and module boundaries work best for agent-assisted development.
  The `good first issue` label marks issues suitable for new contributors
  (human or AI)
- **Plan before implementing**: For non-trivial changes (new modules,
  cross-module refactors, schema changes), write a plan or design comment on the
  issue before coding

## Skills

Claude Code skills are available for common workflows:

### Development workflow

| Skill | Purpose |
|---|---|
| `/tdd` | Test-driven development cycle (red-green-refactor) |
| `/new-module` | Scaffold a new hardware-isolated module with tests |
| `/new-migration` | Add a SQLite schema migration to storage.py |
| `/pr-checklist` | Pre-PR verification (tests, lint, types, docs, risk-tier checks) |
| `/spec` | Generate a structured spec (decision table, state diagram, or EARS) from a GitHub issue |
| `/integration-test` | Run federation integration tests (Layer 1/2/3) |

### Review and compliance

| Skill | Purpose |
|---|---|
| `/data-license` | Review code changes against the data licensing policy |
| `/release-notes` | Draft a RELEASES.md entry from commits since last stage tag |
| `/skill-eval` | Run evaluation test cases against a skill to measure quality |

### Reference and operations

| Skill | Purpose |
|---|---|
| `/domain` | Sailing instrument reference — Signal K paths, NMEA 2000 PGNs, racing concepts |
| `/architecture` | Codebase comprehension — module map, data flow, complexity hotspots, risk tiers |
| `/deploy-pi` | Pi deployment reference and service architecture |
| `/diagnose` | Systematic Pi troubleshooting runbook — checks all subsystems |

### Ideation and planning

| Skill | Purpose |
|---|---|
| `/ideate` | Capture half-baked ideas into the ideation log for future reference |

## Risk Tiers and Module Review

A PR's risk tier is the **highest** tier of any file it touches. The
`/pr-checklist` skill resolves the tier automatically. New modules default
to **Standard** until explicitly classified.

| Tier | Modules | Verification |
|---|---|---|
| **Critical** | `auth.py`, `peer_auth.py`, `federation.py`, `storage.py` (migrations), `can_reader.py` | TDD + integration tests + `/data-license` + spec review before implementation |
| **High** | `sk_reader.py`, `peer_api.py`, `peer_client.py`, `export.py`, `transcribe.py`, `boat_settings.py` | TDD + integration tests where applicable |
| **Standard** | `web.py`, `polar.py`, `external.py`, `races.py`, `triggers.py`, `maneuver_detector.py`, `race_classifier.py`, `courses.py`, `session_matching.py` | TDD + standard PR checklist |
| **Low** | Templates, CSS, JS, docs, config, scripts | Smoke test / visual check |

### Module-specific notes

| Module | Notes |
|---|---|
| `storage.py` | Schema migrations must be backwards-compatible; FK constraints affect cascading deletes |
| `auth.py`, `peer_auth.py` | Security-sensitive; changes affect all authenticated endpoints and inter-boat signing |
| `federation.py` | Ed25519 identity and co-op charter management; cryptographic operations |
| `web.py` | Large file (~6,800 lines); check auth decorators and rate limits on new endpoints |
| `export.py` | Data leaves the system; check GPS precision and data policy compliance |
| `main.py` | Wiring only — no business logic; changes affect startup order |
| `cameras.py`, `sk_reader.py`, `can_reader.py` | Hardware-isolated; well-contained |

## Review Process

- PRs require CI to pass (tests, lint, format) and at least one maintainer approval
- Expect a response within a few days; complex changes may take longer
- Maintainers may request changes, ask questions, or suggest alternatives
- Once approved, the maintainer will merge via squash-and-merge

## License

- **Code**: AGPLv3 (see `LICENSE`)
- **Data**: Governed by the data licensing policy (`docs/data-licensing.md`).
  The code license and data policy are independent — contributing code does
  not grant rights to user data, and using the data co-op does not grant
  rights beyond what AGPLv3 allows
