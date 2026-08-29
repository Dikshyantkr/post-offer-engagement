"""Integration tests for the Module 2 REST API.

Fixtures (`client`, `recruiter_id`, `make_candidate`) and the row-leak guard
come from conftest.py; constants and payload builders from helpers.py.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.enums import (
    FollowUpPriority,
    FollowUpSource,
    FollowUpStatus,
    RiskLevel,
    RiskSource,
)
from app.models import AuditLog, Candidate, FollowUpAction
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


# ---------------------------------------------------------------------------
# GET /candidates?sort=risk — the triage view
#
# The product is "40 pending joiners turned into the five worth a phone call
# this morning". That list is unbuildable while the only ordering is by date.
# ---------------------------------------------------------------------------

_BAND_ORDER = {"high": 0, "medium": 1, "low": 2}


def _listing(client, **params) -> list[dict]:
    resp = client.get(f"{API}/candidates", params={"limit": 100, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def test_default_sort_is_still_joining_date(client):
    dates = [c["joining_date"] for c in _listing(client)]
    assert dates == sorted(dates), "the default listing must keep reading as a calendar"


def test_sort_risk_orders_by_band_then_score(client):
    items = _listing(client, sort="risk")
    keys = [(_BAND_ORDER[c["risk_level"]], -c["risk_score_base"]) for c in items]
    assert keys == sorted(keys)


def test_sort_risk_puts_an_ai_raised_candidate_above_a_higher_scoring_rule_candidate(
    client, make_candidate
):
    """The reason the sort is two keys and not just risk_score_base.

    risk_score_base is the RULE floor. An AI assessment can raise a candidate's
    band above it, so the candidate whose counter-offer signal is only visible
    in a WhatsApp message carries a MEDIUM-band score and a HIGH badge. Sorting
    on the score alone would file her below every rule-flagged candidate —
    exactly backwards, since she is the one worth calling first.
    """
    ai_raised = make_candidate(
        offer_date=(date.today() - timedelta(days=10)).isoformat(),
        joining_date=(date.today() + timedelta(days=40)).isoformat(),
    )
    rule_high = make_candidate(
        offer_date=(date.today() - timedelta(days=10)).isoformat(),
        joining_date=(date.today() + timedelta(days=40)).isoformat(),
    )
    with SessionLocal() as db:
        raised = db.get(Candidate, uuid.UUID(ai_raised["id"]))
        raised.risk_level, raised.risk_source, raised.risk_score_base = (
            RiskLevel.HIGH,
            RiskSource.AI,
            50.1,  # a MEDIUM-band floor
        )
        plain = db.get(Candidate, uuid.UUID(rule_high["id"]))
        plain.risk_level, plain.risk_source, plain.risk_score_base = (
            RiskLevel.MEDIUM,
            RiskSource.RULE,
            68.0,  # a higher score, but a lower band
        )
        db.commit()

    order = [c["id"] for c in _listing(client, sort="risk")]
    assert order.index(ai_raised["id"]) < order.index(rule_high["id"])


def test_sort_risk_respects_filters_and_the_pagination_envelope(client):
    body = client.get(
        f"{API}/candidates", params={"sort": "risk", "risk_level": "high", "limit": 5}
    ).json()

    assert set(body.keys()) == {"items", "total", "limit", "offset"}
    assert all(c["risk_level"] == "high" for c in body["items"])
    scores = [c["risk_score_base"] for c in body["items"]]
    assert scores == sorted(scores, reverse=True)


def test_sort_risk_paginates_without_repeating_or_skipping(client):
    first = [c["id"] for c in _listing(client, sort="risk", limit=10, offset=0)]
    second = [c["id"] for c in _listing(client, sort="risk", limit=10, offset=10)]

    assert len(first) == 10
    assert not set(first) & set(second), "a stable sort must not repeat rows across pages"


def test_unknown_sort_value_returns_422(client):
    resp = client.get(f"{API}/candidates", params={"sort": "name"})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# PATCH /follow-up-actions/{id}
#
# Module 7's action queue closes actions through this endpoint, and it owns
# state beyond the field it is handed: completed_at is set when an action is
# resolved and cleared when it is reopened. Nothing else in the app writes that
# column, so if this is wrong the queue silently loses its record of when work
# was actually done.
# ---------------------------------------------------------------------------


def _make_action(candidate_id: str, **overrides) -> str:
    """Insert a follow-up action directly.

    Deliberately not routed through the automation sweep: these tests are about
    this endpoint's own state transitions, and going through the sweep would
    drag an LLM provider and two automation rules into a test that depends on
    neither. Cleaned up by make_candidate's teardown, which deletes
    follow_up_actions by candidate_id.
    """
    fields = {
        "candidate_id": uuid.UUID(candidate_id),
        "title": "Call about the notice period",
        "description": "Seeded by a test.",
        "due_date": date.today(),
        "priority": FollowUpPriority.HIGH,
        "status": FollowUpStatus.OPEN,
        "source": FollowUpSource.MANUAL,
    }
    fields.update(overrides)
    with SessionLocal() as db:
        action = FollowUpAction(**fields)
        db.add(action)
        db.commit()
        return str(action.id)


def _patch_action(client, action_id: str, **payload):
    return client.patch(f"{API}/follow-up-actions/{action_id}", json=payload)


def _db_action(action_id: str) -> FollowUpAction:
    with SessionLocal() as db:
        return db.get(FollowUpAction, uuid.UUID(action_id))


@pytest.mark.parametrize("resolved_status", ["done", "dismissed"])
def test_resolving_an_action_stamps_completed_at(client, make_candidate, resolved_status):
    """Both resolutions are terminal and both need a timestamp — dismissing an
    action is a decision someone made at a moment, not an absence of one."""
    candidate = make_candidate()
    action_id = _make_action(candidate["id"])
    assert _db_action(action_id).completed_at is None

    resp = _patch_action(client, action_id, status=resolved_status)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == resolved_status
    assert _db_action(action_id).completed_at is not None


def test_reopening_an_action_clears_completed_at(client, make_candidate):
    """Otherwise a reopened action carries a completion time for work that is
    demonstrably not complete, and any 'closed this week' figure built on that
    column is wrong."""
    candidate = make_candidate()
    action_id = _make_action(candidate["id"])
    _patch_action(client, action_id, status="done")
    assert _db_action(action_id).completed_at is not None

    resp = _patch_action(client, action_id, status="open")

    assert resp.status_code == 200, resp.text
    assert _db_action(action_id).completed_at is None


def test_editing_other_fields_leaves_completed_at_untouched(client, make_candidate):
    """completed_at moves only on a status change. Re-prioritising a finished
    action must not restamp when it was finished."""
    candidate = make_candidate()
    action_id = _make_action(candidate["id"])
    _patch_action(client, action_id, status="done")
    stamped = _db_action(action_id).completed_at

    resp = _patch_action(client, action_id, priority="urgent", title="Renamed")

    assert resp.status_code == 200, resp.text
    row = _db_action(action_id)
    assert row.priority == FollowUpPriority.URGENT
    assert row.title == "Renamed"
    assert row.completed_at == stamped
    assert row.status == FollowUpStatus.DONE


def test_patch_updates_every_editable_field(client, make_candidate):
    candidate = make_candidate()
    action_id = _make_action(candidate["id"])
    due = (date.today() + timedelta(days=3)).isoformat()

    resp = _patch_action(
        client,
        action_id,
        title="Escalate to the hiring manager",
        description="Relocation still unresolved.",
        due_date=due,
        priority="urgent",
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Escalate to the hiring manager"
    assert body["description"] == "Relocation still unresolved."
    assert body["due_date"] == due
    assert body["priority"] == "urgent"


def test_patch_leaves_untouched_fields_alone(client, make_candidate):
    """It is a partial update: fields absent from the body keep their values,
    including the ones this endpoint cannot edit at all."""
    candidate = make_candidate()
    action_id = _make_action(
        candidate["id"], generated_message="Hi there,", rule_key="imminent_silence"
    )

    _patch_action(client, action_id, priority="low")

    row = _db_action(action_id)
    assert row.priority == FollowUpPriority.LOW
    assert row.title == "Call about the notice period"
    assert row.description == "Seeded by a test."
    assert row.generated_message == "Hi there,", "the drafted message is not editable here"
    assert row.rule_key == "imminent_silence", "nor is the rule that created it"
    assert row.source == FollowUpSource.MANUAL


def test_empty_patch_is_a_no_op(client, make_candidate):
    candidate = make_candidate()
    action_id = _make_action(candidate["id"])
    before = _db_action(action_id)

    resp = _patch_action(client, action_id)

    assert resp.status_code == 200, resp.text
    after = _db_action(action_id)
    assert (after.title, after.status, after.completed_at) == (
        before.title,
        before.status,
        before.completed_at,
    )


def test_patch_unknown_action_returns_404_envelope(client):
    resp = _patch_action(client, str(uuid.uuid4()), status="done")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_patch_invalid_status_returns_422(client, make_candidate):
    candidate = make_candidate()
    action_id = _make_action(candidate["id"])

    resp = _patch_action(client, action_id, status="completed")

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("field", ["title", "priority", "status"])
def test_explicit_null_for_a_required_field_returns_422(client, make_candidate, field):
    """A NOT NULL column must fail validation, not surface as a raw
    IntegrityError from the database."""
    candidate = make_candidate()
    action_id = _make_action(candidate["id"])

    resp = _patch_action(client, action_id, **{field: None})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_a_resolved_action_leaves_the_open_queue(client, make_candidate):
    """What the queue actually depends on: the status filter and this endpoint
    agreeing, so a closed action stops being served."""
    candidate = make_candidate()
    action_id = _make_action(candidate["id"])

    def _open_ids() -> set[str]:
        resp = client.get(f"{API}/follow-up-actions", params={"status": "open", "limit": 100})
        assert resp.status_code == 200, resp.text
        return {a["id"] for a in resp.json()["items"]}

    assert action_id in _open_ids()
    _patch_action(client, action_id, status="done")
    assert action_id not in _open_ids()
