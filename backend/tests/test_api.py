"""Integration tests for the Module 2 REST API.

Fixtures (`client`, `recruiter_id`, `make_candidate`) and the row-leak guard
come from conftest.py; constants and payload builders from helpers.py.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog
from tests.helpers import API, candidate_payload

# ---------------------------------------------------------------------------
# POST /candidates materialises 8 candidate_stages rows with correct dates
# ---------------------------------------------------------------------------


def test_create_candidate_materialises_eight_stages_with_correct_dates(client, make_candidate):
    offer_date = date(2026, 1, 1)
    joining_date = offer_date + timedelta(days=90)  # ample window, no compression
    candidate = make_candidate(
        name="Stage Check Candidate",
        offer_date=offer_date.isoformat(),
        joining_date=joining_date.isoformat(),
    )

    stages_resp = client.get(f"{API}/candidates/{candidate['id']}/stages")
    assert stages_resp.status_code == 200, stages_resp.text
    stages = stages_resp.json()
    assert len(stages) == 8

    by_key = {s["stage_key"]: s for s in stages}
    assert by_key["offer_accepted"]["due_date"] == offer_date.isoformat()
    assert by_key["welcome"]["due_date"] == (offer_date + timedelta(days=1)).isoformat()
    assert by_key["documentation"]["due_date"] == (offer_date + timedelta(days=3)).isoformat()
    assert by_key["manager_intro"]["due_date"] == (offer_date + timedelta(days=21)).isoformat()
    assert by_key["team_context"]["due_date"] == (offer_date + timedelta(days=35)).isoformat()
    assert by_key["relocation_check"]["due_date"] == (joining_date - timedelta(days=25)).isoformat()
    assert by_key["pre_joining_checkin"]["due_date"] == (joining_date - timedelta(days=10)).isoformat()
    assert by_key["joining"]["due_date"] == joining_date.isoformat()

    ordered = sorted(stages, key=lambda s: s["sequence_order"])
    due_dates = [s["due_date"] for s in ordered]
    assert due_dates == sorted(due_dates)
    assert all(s["status"] == "pending" for s in stages)


def test_create_candidate_compression_path_still_monotonic_via_api(client, make_candidate):
    """30-day notice triggers compute_stage_schedule's compression path —
    exercise it through the live API too, not just the pure unit test."""
    offer_date = date(2026, 3, 1)
    joining_date = offer_date + timedelta(days=30)
    candidate = make_candidate(
        name="Compression Check Candidate",
        offer_date=offer_date.isoformat(),
        joining_date=joining_date.isoformat(),
    )

    stages = client.get(f"{API}/candidates/{candidate['id']}/stages").json()
    stages.sort(key=lambda s: s["sequence_order"])
    due_dates = [s["due_date"] for s in stages]
    assert due_dates == sorted(due_dates)
    assert due_dates[0] == offer_date.isoformat()
    assert due_dates[-1] == joining_date.isoformat()


# ---------------------------------------------------------------------------
# PATCH offer_date/joining_date reschedules the materialised stage rows
# ---------------------------------------------------------------------------


def _due_by_key(client, candidate_id: str) -> dict[str, str]:
    return {s["stage_key"]: s["due_date"] for s in client.get(f"{API}/candidates/{candidate_id}/stages").json()}


def test_patch_joining_date_reschedules_joining_anchored_stages(client, make_candidate):
    offer_date = date(2026, 1, 1)
    joining_date = offer_date + timedelta(days=90)
    candidate = make_candidate(
        offer_date=offer_date.isoformat(), joining_date=joining_date.isoformat()
    )

    before = _due_by_key(client, candidate["id"])
    assert before["joining"] == joining_date.isoformat()

    new_joining = joining_date + timedelta(days=60)
    resp = client.patch(
        f"{API}/candidates/{candidate['id']}", json={"joining_date": new_joining.isoformat()}
    )
    assert resp.status_code == 200, resp.text

    after = _due_by_key(client, candidate["id"])
    assert after["joining"] == new_joining.isoformat()
    assert after["relocation_check"] == (new_joining - timedelta(days=25)).isoformat()
    assert after["pre_joining_checkin"] == (new_joining - timedelta(days=10)).isoformat()

    # Offer-anchored stages are unaffected while the window stays wide enough.
    for key in ("offer_accepted", "welcome", "documentation", "manager_intro", "team_context"):
        assert after[key] == before[key]

    ordered = sorted(
        client.get(f"{API}/candidates/{candidate['id']}/stages").json(),
        key=lambda s: s["sequence_order"],
    )
    dates = [s["due_date"] for s in ordered]
    assert dates == sorted(dates)


def test_patch_offer_date_reschedules_offer_anchored_stages(client, make_candidate):
    offer_date = date(2026, 1, 1)
    joining_date = offer_date + timedelta(days=90)
    candidate = make_candidate(
        offer_date=offer_date.isoformat(), joining_date=joining_date.isoformat()
    )

    new_offer = offer_date + timedelta(days=10)
    resp = client.patch(
        f"{API}/candidates/{candidate['id']}", json={"offer_date": new_offer.isoformat()}
    )
    assert resp.status_code == 200, resp.text

    after = _due_by_key(client, candidate["id"])
    assert after["offer_accepted"] == new_offer.isoformat()
    assert after["welcome"] == (new_offer + timedelta(days=1)).isoformat()
    assert after["team_context"] == (new_offer + timedelta(days=35)).isoformat()
    # joining_date untouched, so its anchored stages hold still.
    assert after["joining"] == joining_date.isoformat()


def test_patch_to_short_notice_compresses_and_stays_monotonic(client, make_candidate):
    """Shrinking the window via PATCH must re-run compression, not leave
    stage dates running backwards."""
    offer_date = date(2026, 1, 1)
    candidate = make_candidate(
        offer_date=offer_date.isoformat(),
        joining_date=(offer_date + timedelta(days=90)).isoformat(),
    )

    tight_joining = offer_date + timedelta(days=30)
    resp = client.patch(
        f"{API}/candidates/{candidate['id']}", json={"joining_date": tight_joining.isoformat()}
    )
    assert resp.status_code == 200, resp.text

    ordered = sorted(
        client.get(f"{API}/candidates/{candidate['id']}/stages").json(),
        key=lambda s: s["sequence_order"],
    )
    dates = [s["due_date"] for s in ordered]
    assert dates == sorted(dates), f"stage dates went backwards after PATCH: {dates}"
    assert dates[0] == offer_date.isoformat()
    assert dates[-1] == tight_joining.isoformat()
    # Compression actually bit: team_context can no longer sit at offer+35.
    assert dates[4] < (offer_date + timedelta(days=35)).isoformat()


# ---------------------------------------------------------------------------
# Filters — risk_level in particular proves the uppercase-Postgres-enum-label
# quirk is handled correctly end to end through the real API.
# ---------------------------------------------------------------------------


def test_filter_by_risk_level(client):
    resp = client.get(f"{API}/candidates", params={"risk_level": "high", "limit": 100})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] > 0
    assert body["items"], "expected at least one seeded high-risk candidate"
    assert all(item["risk_level"] == "high" for item in body["items"])


def test_filter_by_risk_level_invalid_value_returns_422_not_a_db_error(client):
    resp = client.get(f"{API}/candidates", params={"risk_level": "extreme"})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "validation_error"


def test_filter_by_joining_within_days(client, make_candidate):
    today = date.today()
    near = make_candidate(
        offer_date=(today - timedelta(days=20)).isoformat(),
        joining_date=(today + timedelta(days=5)).isoformat(),
    )
    far = make_candidate(
        offer_date=(today - timedelta(days=20)).isoformat(),
        joining_date=(today + timedelta(days=90)).isoformat(),
    )

    resp = client.get(f"{API}/candidates", params={"joining_within_days": 7, "limit": 100})
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert near["id"] in ids
    assert far["id"] not in ids


# ---------------------------------------------------------------------------
# POST interaction updates last_interaction_at in the same transaction
# ---------------------------------------------------------------------------


def test_post_interaction_updates_last_interaction_at(client, make_candidate):
    candidate = make_candidate()
    assert candidate["last_interaction_at"] is None

    resp = client.post(
        f"{API}/candidates/{candidate['id']}/interactions",
        json={
            "channel": "whatsapp",
            "direction": "inbound",
            "content": "test message for integration test",
            "occurred_at": "2026-01-15T10:00:00Z",
        },
    )
    assert resp.status_code == 201, resp.text

    detail = client.get(f"{API}/candidates/{candidate['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["last_interaction_at"] is not None
    assert body["last_interaction_at"].startswith("2026-01-15T10:00:00")
    assert len(body["interactions"]) == 1


def test_post_interaction_does_not_move_last_interaction_at_backwards(client, make_candidate):
    candidate = make_candidate()

    recent = client.post(
        f"{API}/candidates/{candidate['id']}/interactions",
        json={
            "channel": "email",
            "direction": "outbound",
            "content": "recent outreach",
            "occurred_at": "2026-02-01T09:00:00Z",
        },
    )
    assert recent.status_code == 201, recent.text

    backdated = client.post(
        f"{API}/candidates/{candidate['id']}/interactions",
        json={
            "channel": "email",
            "direction": "inbound",
            "content": "backdated reply logged late",
            "occurred_at": "2026-01-01T09:00:00Z",
        },
    )
    assert backdated.status_code == 201, backdated.text

    detail = client.get(f"{API}/candidates/{candidate['id']}").json()
    assert detail["last_interaction_at"].startswith("2026-02-01T09:00:00")
    assert len(detail["interactions"]) == 2


# ---------------------------------------------------------------------------
# PATCH risk_level flips risk_source to hr_override and writes an audit row
# ---------------------------------------------------------------------------


def test_patch_risk_level_sets_hr_override_and_writes_audit_row(client, make_candidate):
    candidate = make_candidate()
    assert candidate["risk_source"] == "rule"

    resp = client.patch(
        f"{API}/candidates/{candidate['id']}",
        json={"risk_level": "high"},
        headers={"X-Actor": "test-recruiter"},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["risk_level"] == "high"
    assert updated["risk_source"] == "hr_override"

    with SessionLocal() as db:
        rows = list(
            db.scalars(
                select(AuditLog).where(
                    AuditLog.entity_type == "candidate",
                    AuditLog.entity_id == uuid.UUID(candidate["id"]),
                    AuditLog.action == "update",
                )
            )
        )

    assert rows, "expected an audit_log row for the risk_level override"
    latest = max(rows, key=lambda r: r.created_at)
    assert latest.actor == "test-recruiter"
    assert latest.before["risk_level"] == "low"
    assert latest.after["risk_level"] == "high"
    assert latest.after["risk_source"] == "hr_override"


def test_patch_without_x_actor_header_defaults_to_system(client, make_candidate):
    candidate = make_candidate()
    resp = client.patch(
        f"{API}/candidates/{candidate['id']}",
        json={"notes": "no actor header on this request"},
    )
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        rows = list(
            db.scalars(
                select(AuditLog).where(
                    AuditLog.entity_type == "candidate",
                    AuditLog.entity_id == uuid.UUID(candidate["id"]),
                )
            )
        )
    assert any(r.actor == "system" for r in rows)


# ---------------------------------------------------------------------------
# Error envelope shape: {"error": {code, message, details}}
# ---------------------------------------------------------------------------


def test_get_missing_candidate_returns_404_envelope(client):
    resp = client.get(f"{API}/candidates/{uuid.uuid4()}")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"]["code"] == "not_found"
    assert isinstance(body["error"]["message"], str)


def test_create_candidate_bad_email_returns_422_envelope(client, recruiter_id):
    payload = candidate_payload(recruiter_id)
    payload["email"] = "not-an-email"
    resp = client.post(f"{API}/candidates", json=payload)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert isinstance(body["error"]["details"], list)
    assert "stack" not in str(body).lower() and "traceback" not in str(body).lower()


def test_create_candidate_joining_before_offer_returns_422(client, recruiter_id):
    payload = candidate_payload(
        recruiter_id,
        offer_date=date(2026, 6, 1).isoformat(),
        joining_date=date(2026, 5, 1).isoformat(),
    )
    resp = client.post(f"{API}/candidates", json=payload)
    assert resp.status_code == 422, resp.text


def test_duplicate_email_returns_409_envelope(client, recruiter_id, make_candidate):
    existing = make_candidate()

    resp = client.post(f"{API}/candidates", json=candidate_payload(recruiter_id, email=existing["email"]))
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"]["code"] == "conflict"
    # The envelope must not leak SQL, constraint names or parameter values.
    lowered = str(body).lower()
    assert "insert" not in lowered and "constraint" not in lowered and "traceback" not in lowered


# ---------------------------------------------------------------------------
# Stage completion: transition, engagement_status advance, audit rows
# ---------------------------------------------------------------------------


def _stage_id_for(client, candidate_id: str, stage_key: str) -> str:
    stages = client.get(f"{API}/candidates/{candidate_id}/stages").json()
    return next(s["stage_id"] for s in stages if s["stage_key"] == stage_key)


def test_complete_stage_transitions_then_rejects_double_complete(client, make_candidate):
    candidate = make_candidate()
    stages = client.get(f"{API}/candidates/{candidate['id']}/stages").json()
    first_stage = min(stages, key=lambda s: s["sequence_order"])
    assert first_stage["status"] == "pending"

    resp = client.post(
        f"{API}/candidates/{candidate['id']}/stages/{first_stage['stage_id']}/complete",
        headers={"X-Actor": "recruiter-x"},
    )
    assert resp.status_code == 200, resp.text
    completed = resp.json()
    assert completed["status"] == "completed"
    assert completed["completed_by"] == "recruiter-x"
    assert completed["completed_at"] is not None

    again = client.post(
        f"{API}/candidates/{candidate['id']}/stages/{first_stage['stage_id']}/complete"
    )
    assert again.status_code == 400, again.text
    assert again.json()["error"]["code"] == "bad_state_transition"


def test_complete_stage_advances_engagement_status(client, make_candidate):
    candidate = make_candidate()
    assert candidate["engagement_status"] == "offer_accepted"

    resp = client.post(
        f"{API}/candidates/{candidate['id']}/stages/{_stage_id_for(client, candidate['id'], 'welcome')}/complete",
        headers={"X-Actor": "recruiter-y"},
    )
    assert resp.status_code == 200, resp.text

    detail = client.get(f"{API}/candidates/{candidate['id']}").json()
    assert detail["engagement_status"] == "welcome_sent"

    with SessionLocal() as db:
        rows = list(
            db.scalars(
                select(AuditLog).where(
                    AuditLog.entity_id == uuid.UUID(candidate["id"]),
                    AuditLog.action == "engagement_status_advance",
                )
            )
        )
    assert len(rows) == 1
    assert rows[0].entity_type == "candidate"
    assert rows[0].actor == "recruiter-y"
    assert rows[0].before["engagement_status"] == "offer_accepted"
    assert rows[0].after["engagement_status"] == "welcome_sent"


def test_completing_an_earlier_stage_does_not_regress_engagement_status(client, make_candidate):
    """Furthest-along completed stage wins, so back-filling an earlier stage
    must not walk the status backwards."""
    candidate = make_candidate()

    client.post(
        f"{API}/candidates/{candidate['id']}/stages/{_stage_id_for(client, candidate['id'], 'manager_intro')}/complete"
    )
    assert client.get(f"{API}/candidates/{candidate['id']}").json()["engagement_status"] == "manager_intro"

    client.post(
        f"{API}/candidates/{candidate['id']}/stages/{_stage_id_for(client, candidate['id'], 'welcome')}/complete"
    )
    assert client.get(f"{API}/candidates/{candidate['id']}").json()["engagement_status"] == "manager_intro"


def test_completing_joining_stage_does_not_set_status_joined(client, make_candidate):
    """`joining` has no engagement_status mapping — JOINED comes from
    final_outcome, not from ticking the last stage off."""
    candidate = make_candidate()

    client.post(
        f"{API}/candidates/{candidate['id']}/stages/{_stage_id_for(client, candidate['id'], 'pre_joining_checkin')}/complete"
    )
    client.post(
        f"{API}/candidates/{candidate['id']}/stages/{_stage_id_for(client, candidate['id'], 'joining')}/complete"
    )

    detail = client.get(f"{API}/candidates/{candidate['id']}").json()
    assert detail["engagement_status"] == "pre_joining_checkin"
    assert detail["final_outcome"] == "pending"


# ---------------------------------------------------------------------------
# Pagination envelope + max limit enforcement
# ---------------------------------------------------------------------------


def test_candidates_list_pagination_envelope(client):
    resp = client.get(f"{API}/candidates", params={"limit": 5, "offset": 0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"items", "total", "limit", "offset"}
    assert body["limit"] == 5
    assert len(body["items"]) <= 5
    assert body["total"] >= len(body["items"])


def test_candidates_list_limit_over_max_returns_422(client):
    resp = client.get(f"{API}/candidates", params={"limit": 101})
    assert resp.status_code == 422, resp.text
