---
name: tdd
description: HelmLog-specific test patterns and the pre-existing-error allowlist for ruff/mypy. The Red-Green-Refactor cycle and lint commands are already mandated in CLAUDE.md — this skill encodes only the patterns and known-pre-existing-errors list that aren't recoverable from existing tests at a glance. TRIGGER when writing or modifying Python source code in src/helmlog/. DO NOT trigger for documentation, config, templates, CSS/JS, skill definitions, or changes that don't affect runtime behavior.
---

# TDD — HelmLog patterns

CLAUDE.md already mandates: failing test → implement → green → lint. This
skill encodes only the project-specific bits that aren't obvious from
existing tests:

## Test patterns

**Storage tests** — use the shared `storage` fixture from `conftest.py`
(in-memory SQLite, fully migrated; never construct `Storage` by hand in a
test):

```python
@pytest.mark.asyncio
async def test_my_feature(storage: Storage) -> None:
    # storage is ready with all migrations applied
    ...
```

**Web route tests** — use `httpx.AsyncClient` with `ASGITransport`, not the
sync `TestClient`:

```python
@pytest.mark.asyncio
async def test_my_endpoint(storage: Storage) -> None:
    from helmlog.web import create_app
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/my-endpoint")
        assert resp.status_code == 200
```

**Hardware mocking** — patch hardware modules at the import site so the
test never touches real devices:

```python
with patch("helmlog.cameras.httpx.AsyncClient") as mock_client:
    ...
```

Hardware modules to mock: `audio.py`, `can_reader.py`, `sk_reader.py`,
`cameras.py`.

## Pre-existing errors to ignore

Do not fix these unless explicitly asked — they are tracked separately:

- `web.py`: `Item "None" of "datetime | None" has no attribute "isoformat"`
- `web.py`: `Item "None" of "AudioRecorder | None" has no attribute "stop"` (×2)
- `main.py`: `Unused "type: ignore" comment`
- `storage.py`: 2 pre-existing E501 line-length violations
