"""Shared fixtures for every API test module.

Living in conftest.py rather than a single test file means the
`_no_row_leaks` guard covers any test module added later, not just the one
it was originally written for.

These tests hit a real database through the actual FastAPI app (no
dependency overrides) — run against Postgres inside the container, never
SQLite, since several assertions (enum filters in particular) only catch
what they're meant to against the real database.

Because the app commits through its own sessions, tests cannot wrap
themselves in a rollback-only transaction. Instead every candidate a test
creates goes through the `make_candidate` factory, which tracks it and
deletes it at teardown; `_no_row_leaks` then asserts the database is back
to its pre-test row counts, so a test that forgets the factory fails loudly
instead of silently polluting the seeded data.
"""

from __future__ import annotations

import os

# Set before any `app.*` import, because pydantic-settings reads the
# environment once, when app.config is first imported. The TestClient below
# runs the real FastAPI lifespan, which would otherwise start APScheduler and
# let a background thread fire real sweeps — creating follow-up actions no
# test asked for, spending provider quota, and tripping the row-leak guard.
os.environ.setdefault("RUN_SCHEDULER", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.routers.ai import get_llm_provider  # noqa: E402
from tests.helpers import (  # noqa: E402
    API,
    FakeProvider,
    candidate_payload,
    purge_candidates,
    row_counts,
)

assert not settings.run_scheduler, (
    "RUN_SCHEDULER must be false for the test run — see the note above."
)


@pytest.fixture(scope="session", autouse=True)
def _no_row_leaks():
    """Fail the run if any test leaves rows behind in the seeded database."""
    before = row_counts()
    yield
    after = row_counts()
    leaked = {
        table: (before[table], after[table]) for table in before if before[table] != after[table]
    }
    assert not leaked, f"tests leaked rows (table: before -> after): {leaked}"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def recruiter_id(client: TestClient) -> str:
    resp = client.get(f"{API}/recruiters", params={"limit": 1})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items, "expected at least one seeded recruiter"
    return items[0]["id"]


@pytest.fixture
def make_candidate(client: TestClient, recruiter_id: str):
    """Create candidates through the API, tracking them for teardown.

    Every successful POST /candidates in the suite must go through here —
    anything created directly will trip the _no_row_leaks guard.
    """
    created: list[str] = []

    def _make(**overrides) -> dict:
        resp = client.post(f"{API}/candidates", json=candidate_payload(recruiter_id, **overrides))
        assert resp.status_code == 201, resp.text
        body = resp.json()
        created.append(body["id"])
        return body

    yield _make
    purge_candidates(created)


@pytest.fixture
def use_provider():
    """Swap the LLM provider for the duration of one test via FastAPI's
    dependency_overrides — the real GeminiProvider is never constructed, and
    no test in this suite reaches the Gemini API.

    Shared by the Module 5 engine tests and the Module 6 sweep tests, which is
    why it lives here rather than in either module.
    """

    def _use(*responses: str | Exception) -> FakeProvider:
        provider = FakeProvider(*responses)
        app.dependency_overrides[get_llm_provider] = lambda: provider
        return provider

    yield _use
    app.dependency_overrides.pop(get_llm_provider, None)
