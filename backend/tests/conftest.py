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

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import API, candidate_payload, purge_candidates, row_counts


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
