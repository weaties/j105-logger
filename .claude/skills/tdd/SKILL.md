---
name: tdd
description: Use test-driven development when implementing new features or fixing bugs. TRIGGER when writing or modifying Python source code in src/helmlog/ — new functionality, bug fixes, or refactors that change behavior. DO NOT trigger for documentation, config, templates, CSS/JS, skill definitions, or changes that don't affect runtime behavior.
---

# Test-Driven Development (Red-Green-Refactor)

Always follow this cycle when implementing new functionality or fixing bugs.

## 0. Assess Scope — Risk-Tier-Aware Test Depth

Before writing any test, identify the module being changed and resolve its risk tier
from CLAUDE.md. The tier determines how thorough the tests must be:

| Tier | Test depth |
|---|---|
| **Critical** (`auth.py`, `peer_auth.py`, `federation.py`, `storage.py` migrations, `can_reader.py`) | Exhaustive: every happy path, every edge case, every error path, every boundary condition. If a decision table exists (or should exist), each row = one test case. |
| **High** (`sk_reader.py`, `peer_api.py`, `peer_client.py`, `export.py`, `transcribe.py`, `boat_settings.py`) | Happy path + key edge cases + all error paths. |
| **Standard** (most modules) | Happy path + one meaningful edge case. |
| **Low** (templates, CSS, JS, docs, config) | Happy path only (if runtime behavior is affected at all). |

A PR's tier is the **highest** tier of any file it touches. When in doubt, go one tier up.

**Critical-tier gate:** If the module is Critical tier, check whether a `/spec` has been
reviewed before proceeding. If not, flag this — specs should precede TDD for Critical modules.

## 1. Red — Write a Failing Test

Write the test **before** writing the implementation.

- Test file: `tests/test_<module>.py`
- Use the `storage` fixture from `conftest.py` for in-memory SQLite
- Use `httpx.AsyncClient` with `ASGITransport` for web route tests
- Mock hardware modules (`audio.py`, `can_reader.py`, `sk_reader.py`, `cameras.py`)
- All tests are `@pytest.mark.asyncio` for async code

```bash
uv run pytest tests/test_<module>.py -xvs
```

Confirm the test **fails** with the expected error (not an import error or fixture issue).

## 2. Green — Write Minimal Code to Pass

Implement just enough to make the failing test pass. No more.

```bash
uv run pytest tests/test_<module>.py -xvs
```

Confirm the test **passes**.

## 3. Refactor — Improve Without Breaking

Clean up the implementation. Then run the **full** test suite:

```bash
uv run pytest
```

All tests must pass.

## 3a. Coverage Delta — Verify New Code Is Covered

After refactoring, run coverage on the module under test:

```bash
uv run pytest --cov=helmlog --cov-report=term-missing tests/test_<module>.py
```

- Check the `Missing` column — any new lines you wrote that appear there are untested code paths.
- If new lines are uncovered, go back to step 1 and add tests for those paths before proceeding.
- The goal is not 100% coverage of the entire module — it's **100% coverage of the code you just wrote or changed**.

### Mutation testing hint (Critical tier only)

If this is a Critical-tier module, ask yourself: would these tests catch a **subtle** bug?
For example:
- An off-by-one in an embargo timestamp comparison (`<=` vs `<`)
- A wrong comparison operator (`!=` instead of `==`)
- A missing auth check that silently allows access
- A swapped argument order in a crypto verification call

Tests should fail if the **logic** is wrong, not just if the code crashes. If a test would
still pass with a flipped condition or deleted guard clause, the test is too weak — strengthen it.

## 4. Lint and Type-Check

```bash
uv run ruff check . && uv run ruff format . && uv run mypy src/
```

**Pre-existing errors to ignore** (do not fix unless explicitly asked):
- `web.py`: `Item "None" of "datetime | None" has no attribute "isoformat"`
- `web.py`: `Item "None" of "AudioRecorder | None" has no attribute "stop"` (x2)
- `main.py`: `Unused "type: ignore" comment`
- `storage.py`: 2 pre-existing E501 line-length violations

## 5. Commit and Chain

Commit the test and implementation together. Use conventional commit format.
If working on a GitHub issue, include the issue number in the commit message:
`feat: description (#332)`

### Cross-skill chaining

After committing, check whether follow-up skills are needed:

- **Module is Critical or High tier AND touches data, PII, or federation?**
  → Run `/data-license` to review changes against the data licensing policy.
- **Module is Critical tier?**
  → Verify that a `/spec` was reviewed before this TDD cycle began. If it wasn't, flag this gap.
- **Federation code was touched** (`federation.py`, `peer_api.py`, `peer_auth.py`, `peer_client.py`)?
  → Run `/integration-test` to validate against the integration test suite.

## Testing Patterns

### Fixture Selection Guide

Choose the right fixture based on what you're testing:

```
What am I testing?
│
├─ Reads/writes SQLite? ──────────→ `storage` fixture (in-memory, fully migrated)
├─ Web routes or API endpoints? ──→ `httpx.AsyncClient` + `ASGITransport`
├─ Federation / peer / co-op? ────→ `fleet` fixture (tests/integration/conftest.py)
│                                    Two boats with real Ed25519 keypairs
├─ Hardware interaction? ─────────→ Mock at module level:
│   ├─ audio.py ──→ patch sounddevice/soundfile
│   ├─ can_reader.py ──→ patch python-can bus
│   ├─ sk_reader.py ──→ patch websockets
│   └─ cameras.py ──→ patch httpx.AsyncClient
├─ File I/O (exports, audio)? ───→ `tmp_path` (pytest built-in)
└─ Pure logic (no I/O)? ─────────→ No fixture needed — direct function calls
```

**Storage tests** — use the `storage` fixture (in-memory SQLite, fully migrated):
```python
@pytest.mark.asyncio
async def test_my_feature(storage: object) -> None:
    from logger.storage import Storage
    assert isinstance(storage, Storage)
    # storage is ready with all migrations applied
```

**Web route tests** — use `httpx.AsyncClient`:
```python
@pytest.mark.asyncio
async def test_my_endpoint(storage: object) -> None:
    from logger.storage import Storage
    assert isinstance(storage, Storage)
    from logger.web import create_app
    app = create_app(storage)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/my-endpoint")
        assert resp.status_code == 200
```

**Hardware mocking** — patch at the module level:
```python
with patch("logger.cameras.httpx.AsyncClient") as mock_client:
    # test camera operations without real hardware
```
