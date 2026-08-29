"""Database-level risk engine tests: the parts compute_base_risk can't cover
on its own — never-contacted silence, hr_override protection, final_outcome
exclusion, and the interaction hook.

Uses the shared conftest fixtures, so every candidate created here is purged
at teardown and the seeded 54 are left untouched.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.enums import FinalOutcome, RiskLevel, RiskSource
from app.models import AuditLog, Candidate
from tests.helpers import API

TODAY = date.today()


def _db_candidate(candidate_id: str) -> Candidate:
    with SessionLocal() as db:
        return db.get(Candidate, uuid.UUID(candidate_id))


def _recompute(client, actor: str = "test-risk") -> dict:
    resp = client.post(f"{API}/risk/recompute", headers={"X-Actor": actor})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Never-contacted candidates count silence from offer_date
# ---------------------------------------------------------------------------


def test_never_contacted_counts_silence_from_offer_date(client, make_candidate):
    """Offered 40 days ago, never contacted -> silent 40 days -> HIGH."""
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=40)).isoformat(),
        joining_date=(TODAY + timedelta(days=50)).isoformat(),
    )
    assert candidate["last_interaction_at"] is None

    _recompute(client)

    row = _db_candidate(candidate["id"])
    assert row.risk_level == RiskLevel.HIGH
    assert row.risk_score_base >= 70.0


def test_freshly_offered_and_never_contacted_is_not_yet_flagged(client, make_candidate):
    """Offered today with a distant joining date — no silence, no stage lag
    yet, so the rules must not flag them."""
    candidate = make_candidate(
        offer_date=TODAY.isoformat(),
        joining_date=(TODAY + timedelta(days=80)).isoformat(),
    )
    _recompute(client)

    row = _db_candidate(candidate["id"])
    assert row.risk_level == RiskLevel.LOW
    assert row.risk_score_base <= 39.0


def test_backdated_offer_flags_medium_via_overdue_first_stage(client, make_candidate):
    """Offered 2 days ago and never touched: offer_accepted (offset 0) and
    welcome (offset +1) are already past due, so the stage-lag rule fires
    even though silence alone would not."""
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=2)).isoformat(),
        joining_date=(TODAY + timedelta(days=80)).isoformat(),
    )
    _recompute(client)

    row = _db_candidate(candidate["id"])
    assert row.risk_level == RiskLevel.MEDIUM


# ---------------------------------------------------------------------------
# Creating an interaction lowers risk, in the same transaction
# ---------------------------------------------------------------------------


def test_logging_an_interaction_lowers_risk(client, make_candidate):
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=40)).isoformat(),
        joining_date=(TODAY + timedelta(days=50)).isoformat(),
    )
    _recompute(client)

    before = _db_candidate(candidate["id"])
    assert before.risk_level == RiskLevel.HIGH
    score_before = before.risk_score_base

    resp = client.post(
        f"{API}/candidates/{candidate['id']}/interactions",
        json={
            "channel": "call",
            "direction": "outbound",
            "content": "Called to check in; all good on their side.",
            "occurred_at": f"{TODAY.isoformat()}T09:00:00Z",
        },
    )
    assert resp.status_code == 201, resp.text

    after = _db_candidate(candidate["id"])
    assert after.risk_score_base < score_before
    assert after.risk_level != RiskLevel.HIGH, "fresh contact should drop them out of HIGH"


def test_interaction_hook_persists_risk_atomically_with_the_interaction(client, make_candidate):
    """The recompute rides the interaction's transaction — if the interaction
    is visible, the new score must be too."""
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=30)).isoformat(),
        joining_date=(TODAY + timedelta(days=60)).isoformat(),
    )
    _recompute(client)
    assert _db_candidate(candidate["id"]).risk_level == RiskLevel.HIGH

    client.post(
        f"{API}/candidates/{candidate['id']}/interactions",
        json={
            "channel": "email",
            "direction": "inbound",
            "content": "all set, see you on the start date",
            "occurred_at": f"{TODAY.isoformat()}T12:00:00Z",
        },
    )

    row = _db_candidate(candidate["id"])
    assert row.last_interaction_at is not None
    assert row.risk_level != RiskLevel.HIGH


# ---------------------------------------------------------------------------
# Blocker signal reaches the rule floor through the API
# ---------------------------------------------------------------------------


def _calm_candidate(make_candidate) -> dict:
    """Offered today, joining far out — LOW on the time-based rules alone,
    so any band change must come from the blocker signal."""
    return make_candidate(
        offer_date=TODAY.isoformat(),
        joining_date=(TODAY + timedelta(days=80)).isoformat(),
    )


def _log_call(client, candidate_id: str, days_ago: int, **fields):
    payload = {
        "channel": "call",
        "direction": "outbound",
        "content": "Call note logged by integration test.",
        "occurred_at": f"{(TODAY - timedelta(days=days_ago)).isoformat()}T10:00:00Z",
    }
    payload.update(fields)
    resp = client.post(f"{API}/candidates/{candidate_id}/interactions", json=payload)
    assert resp.status_code == 201, resp.text
    return resp


def test_counter_offer_call_note_immediately_raises_risk_to_high(client, make_candidate):
    candidate = _calm_candidate(make_candidate)
    _recompute(client)
    assert _db_candidate(candidate["id"]).risk_level == RiskLevel.LOW

    # No recompute call in between — the interaction hook must do it.
    _log_call(
        client,
        candidate["id"],
        days_ago=0,
        blocker_raised=True,
        blocker_category="counter_offer",
    )

    row = _db_candidate(candidate["id"])
    assert row.risk_level == RiskLevel.HIGH, "a logged counter-offer must raise risk immediately"
    assert row.risk_score_base >= 70.0


def test_notice_period_blocker_raises_risk_to_high(client, make_candidate):
    candidate = _calm_candidate(make_candidate)
    _log_call(
        client,
        candidate["id"],
        days_ago=1,
        blocker_raised=True,
        blocker_category="notice_period",
    )
    assert _db_candidate(candidate["id"]).risk_level == RiskLevel.HIGH


def test_other_blocker_category_raises_only_to_medium(client, make_candidate):
    candidate = _calm_candidate(make_candidate)
    _log_call(
        client, candidate["id"], days_ago=1, blocker_raised=True, blocker_category="relocation"
    )
    assert _db_candidate(candidate["id"]).risk_level == RiskLevel.MEDIUM


def test_recruiter_read_worried_raises_to_medium(client, make_candidate):
    candidate = _calm_candidate(make_candidate)
    _log_call(client, candidate["id"], days_ago=1, recruiter_read="worried")
    assert _db_candidate(candidate["id"]).risk_level == RiskLevel.MEDIUM


def test_recruiter_read_unsure_raises_score_but_not_band(client, make_candidate):
    candidate = _calm_candidate(make_candidate)
    _log_call(client, candidate["id"], days_ago=1, recruiter_read="on_track")
    baseline = _db_candidate(candidate["id"])

    unsure_candidate = _calm_candidate(make_candidate)
    _log_call(client, unsure_candidate["id"], days_ago=1, recruiter_read="unsure")
    nudged = _db_candidate(unsure_candidate["id"])

    assert nudged.risk_level == baseline.risk_level == RiskLevel.LOW
    assert nudged.risk_score_base > baseline.risk_score_base


def test_blocker_older_than_thirty_days_is_ignored(client, make_candidate):
    candidate = _calm_candidate(make_candidate)

    # Recent benign contact first, so last_interaction_at stays fresh and
    # silence cannot confound the result.
    _log_call(client, candidate["id"], days_ago=0, recruiter_read="on_track")
    assert _db_candidate(candidate["id"]).risk_level == RiskLevel.LOW

    # A counter-offer from 40 days ago has aged out of the signal window.
    _log_call(
        client,
        candidate["id"],
        days_ago=40,
        blocker_raised=True,
        blocker_category="counter_offer",
    )
    assert _db_candidate(candidate["id"]).risk_level == RiskLevel.LOW, (
        "a blocker outside the 30-day window must not score"
    )

    # The same blocker raised 5 days ago is live and must flag them.
    _log_call(
        client,
        candidate["id"],
        days_ago=5,
        blocker_raised=True,
        blocker_category="counter_offer",
    )
    assert _db_candidate(candidate["id"]).risk_level == RiskLevel.HIGH


def test_blocker_floor_survives_a_full_recompute_sweep(client, make_candidate):
    """The sweep reads interactions too, not just the interaction hook."""
    candidate = _calm_candidate(make_candidate)
    _log_call(
        client,
        candidate["id"],
        days_ago=2,
        blocker_raised=True,
        blocker_category="counter_offer",
    )
    _recompute(client)
    assert _db_candidate(candidate["id"]).risk_level == RiskLevel.HIGH


def test_hr_override_still_beats_a_critical_blocker(client, make_candidate):
    candidate = _calm_candidate(make_candidate)
    _log_call(
        client,
        candidate["id"],
        days_ago=1,
        blocker_raised=True,
        blocker_category="counter_offer",
    )
    assert _db_candidate(candidate["id"]).risk_level == RiskLevel.HIGH

    client.patch(
        f"{API}/candidates/{candidate['id']}",
        json={"risk_level": "low"},
        headers={"X-Actor": "hr-lead"},
    )
    _recompute(client)

    row = _db_candidate(candidate["id"])
    assert row.risk_level == RiskLevel.LOW, "HR overrides everything, blockers included"
    assert row.risk_source == RiskSource.HR_OVERRIDE


# ---------------------------------------------------------------------------
# hr_override wins over the rules
# ---------------------------------------------------------------------------


def test_hr_override_is_not_overwritten_but_score_still_updates(client, make_candidate):
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=40)).isoformat(),
        joining_date=(TODAY + timedelta(days=50)).isoformat(),
    )

    # A human deliberately marks this silent candidate as LOW risk.
    resp = client.patch(
        f"{API}/candidates/{candidate['id']}",
        json={"risk_level": "low"},
        headers={"X-Actor": "hr-lead"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["risk_source"] == "hr_override"

    _recompute(client)

    row = _db_candidate(candidate["id"])
    assert row.risk_level == RiskLevel.LOW, "human decision must survive the recompute"
    assert row.risk_source == RiskSource.HR_OVERRIDE
    # The rule floor is still recorded so the UI can show it beside the badge.
    assert row.risk_score_base >= 70.0


def test_recompute_reports_hr_override_skips(client, make_candidate):
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=40)).isoformat(),
        joining_date=(TODAY + timedelta(days=50)).isoformat(),
    )
    client.patch(
        f"{API}/candidates/{candidate['id']}",
        json={"risk_level": "medium"},
        headers={"X-Actor": "hr-lead"},
    )
    result = _recompute(client)
    assert result["skipped_hr_override"] >= 1


# ---------------------------------------------------------------------------
# Resolved candidates are excluded from scoring
# ---------------------------------------------------------------------------


def test_joined_candidate_is_excluded_from_scoring(client, make_candidate):
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=40)).isoformat(),
        joining_date=(TODAY + timedelta(days=50)).isoformat(),
    )
    client.patch(f"{API}/candidates/{candidate['id']}", json={"final_outcome": "joined"})

    before = _db_candidate(candidate["id"])
    _recompute(client)
    after = _db_candidate(candidate["id"])

    assert after.risk_level == before.risk_level
    assert after.risk_score_base == before.risk_score_base


def test_dropped_out_candidate_is_excluded_from_scoring(client, make_candidate):
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=40)).isoformat(),
        joining_date=(TODAY + timedelta(days=50)).isoformat(),
    )
    client.patch(f"{API}/candidates/{candidate['id']}", json={"final_outcome": "dropped_out"})

    _recompute(client)

    row = _db_candidate(candidate["id"])
    assert row.risk_level == RiskLevel.LOW, "a resolved candidate is not 'at risk'"


def test_resolved_candidates_are_not_counted_in_scanned(client, make_candidate):
    baseline = _recompute(client)["scanned"]

    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=40)).isoformat(),
        joining_date=(TODAY + timedelta(days=50)).isoformat(),
    )
    assert _recompute(client)["scanned"] == baseline + 1

    client.patch(f"{API}/candidates/{candidate['id']}", json={"final_outcome": "joined"})
    assert _recompute(client)["scanned"] == baseline


# ---------------------------------------------------------------------------
# Audit + idempotency
# ---------------------------------------------------------------------------


def test_audit_row_written_once_per_actual_level_change(client, make_candidate):
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=40)).isoformat(),
        joining_date=(TODAY + timedelta(days=50)).isoformat(),
    )

    _recompute(client, actor="risk-bot")
    _recompute(client, actor="risk-bot")  # second pass changes nothing

    with SessionLocal() as db:
        rows = list(
            db.scalars(
                select(AuditLog).where(
                    AuditLog.entity_id == uuid.UUID(candidate["id"]),
                    AuditLog.action == "risk_recompute",
                )
            )
        )

    assert len(rows) == 1, "a no-op recompute must not write a second audit row"
    assert rows[0].actor == "risk-bot"
    assert rows[0].before["risk_level"] == "low"
    assert rows[0].after["risk_level"] == "high"


def test_recompute_is_idempotent(client):
    first = _recompute(client)
    second = _recompute(client)
    assert second["level_changed"] == 0
    assert second["distribution"] == first["distribution"]


def test_recompute_response_shape(client):
    result = _recompute(client)
    assert set(result.keys()) == {
        "scanned",
        "score_updated",
        "level_changed",
        "skipped_hr_override",
        "skipped_ai_higher",
        "distribution",
    }
    assert set(result["distribution"].keys()) == {"low", "medium", "high"}
    assert sum(result["distribution"].values()) == result["scanned"]
