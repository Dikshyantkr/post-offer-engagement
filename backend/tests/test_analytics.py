"""Module 8 analytics tests.

Two kinds of assertion, deliberately, because hardcoding "high_risk_count ==
14" would be a test of today's seed rather than of the SQL, and it would break
tomorrow when the dates move under it:

* **Deltas.** Take a reading, create a candidate with known properties, take
  another. Every metric has to move by exactly the amount that candidate
  justifies — which pins the WHERE clause without depending on the absolute
  seeded numbers.
* **Independent recomputation.** For the aggregates worth distrusting
  (conversion, the interaction average, stalled counts), the expected value is
  computed a second time by a different route — plain Python over the same
  rows, or the automation rule's own query — and the two must agree.

The seeded absolutes that ARE stable (54 candidates, 8 stages, 6 recruiters)
are asserted directly.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import delete, func, select

from app.db import SessionLocal
from app.enums import FinalOutcome, RiskLevel, StageStatus
from app.models import Candidate, CandidateStage, Interaction, JourneyStage, Recruiter
from app.services import automation_service
from app.services.analytics_service import _conversion_pct
from tests.helpers import API, purge_candidates, unique_email

TODAY = date.today()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _overview(client) -> dict:
    resp = client.get(f"{API}/analytics/overview")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _pipeline(client) -> dict:
    resp = client.get(f"{API}/analytics/pipeline")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _recruiters(client) -> list[dict]:
    resp = client.get(f"{API}/analytics/recruiters")
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _stage(pipeline: dict, key: str) -> dict:
    return next(s for s in pipeline["items"] if s["stage_key"] == key)


def _complete_stage(client, candidate_id: str, stage_key: str) -> None:
    stages = client.get(f"{API}/candidates/{candidate_id}/stages").json()
    stage_id = next(s["stage_id"] for s in stages if s["stage_key"] == stage_key)
    resp = client.post(f"{API}/candidates/{candidate_id}/stages/{stage_id}/complete")
    assert resp.status_code == 200, resp.text


@pytest.fixture
def temp_recruiter():
    """A recruiter created for one test and deleted afterwards.

    There is no POST /recruiters, and recruiters are not covered by the
    row-leak guard, so cleanup is this fixture's job — a stray recruiter would
    silently add an empty row to every later run of the recruiters endpoint.
    """
    created: list[uuid.UUID] = []

    def _make(name: str = "Zeta Testrecruiter") -> str:
        with SessionLocal() as db:
            recruiter = Recruiter(name=name, email=unique_email("recruiter"))
            db.add(recruiter)
            db.commit()
            created.append(recruiter.id)
            return str(recruiter.id)

    yield _make

    # Purge this recruiter's candidates before deleting the recruiter, rather
    # than relying on make_candidate's teardown to have run first. Fixtures
    # tear down in reverse setup order, so whether that has happened depends on
    # the order the test declares its arguments in — and a foreign-key
    # violation at teardown fails the test after it has already passed, which
    # is a confusing way to find out about parameter ordering.
    with SessionLocal() as db:
        candidate_ids = [
            str(cid)
            for cid in db.scalars(select(Candidate.id).where(Candidate.recruiter_id.in_(created)))
        ]
    purge_candidates(candidate_ids)

    with SessionLocal() as db:
        db.execute(delete(Recruiter).where(Recruiter.id.in_(created)))
        db.commit()


# ---------------------------------------------------------------------------
# The zero-denominator rule, at its source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "joined,dropped,expected",
    [
        (0, 0, None),  # nothing resolved — no rate exists
        (3, 1, 75.0),
        (0, 5, 0.0),  # a real 0%: five resolved, none joined
        (5, 0, 100.0),
        (1, 2, 33.3),
    ],
)
def test_conversion_pct_handles_every_denominator(joined, dropped, expected):
    """None and 0.0 are different answers and must not be confused. Nothing
    resolved means there is no rate to report; five resolved and none joined
    means the rate is zero."""
    assert _conversion_pct(joined, dropped) == expected


def test_no_analytics_endpoint_divides_by_zero_on_an_empty_slice(client, temp_recruiter):
    """A recruiter holding nothing must appear with zeros and a null rate —
    not be missing, and not 500."""
    recruiter_id = temp_recruiter()

    row = next(r for r in _recruiters(client) if r["recruiter_id"] == recruiter_id)

    assert row["total_offers"] == 0
    assert row["joined"] == row["dropped_out"] == row["pending_count"] == 0
    assert row["conversion_pct"] is None
    assert row["avg_days_since_last_contact"] is None


def test_a_recruiter_with_only_pending_candidates_has_no_conversion_rate(
    client, make_candidate, temp_recruiter
):
    """The case that matters in practice: a new recruiter whose cohort has not
    resolved yet has lost nobody, and reporting 0% would say otherwise."""
    recruiter_id = temp_recruiter()
    make_candidate(recruiter_id=recruiter_id)

    row = next(r for r in _recruiters(client) if r["recruiter_id"] == recruiter_id)

    assert row["pending_count"] == 1
    assert row["joined"] == row["dropped_out"] == 0
    assert row["conversion_pct"] is None
    assert row["avg_days_since_last_contact"] is not None, "a pending candidate has a contact age"


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def test_overview_totals_reconcile_against_the_seeded_database(client):
    body = _overview(client)

    assert body["total_offered"] == 54, "the seeded fixture is 54 candidates"
    assert body["joined"] + body["dropped_out"] + body["pending"] == body["total_offered"], (
        "every candidate is in exactly one outcome"
    )
    assert body["high_risk_count"] + body["medium_risk_count"] <= body["pending"]


def test_a_new_pending_candidate_moves_exactly_the_right_counters(client, make_candidate):
    before = _overview(client)
    make_candidate(
        offer_date=(TODAY - timedelta(days=10)).isoformat(),
        joining_date=(TODAY + timedelta(days=3)).isoformat(),
    )
    after = _overview(client)

    assert after["total_offered"] == before["total_offered"] + 1
    assert after["pending"] == before["pending"] + 1
    assert after["joined"] == before["joined"]
    assert after["dropped_out"] == before["dropped_out"]
    # Joining in 3 days lands inside all three horizons.
    for days in (7, 15, 30):
        assert after[f"joining_next_{days}_days"] == before[f"joining_next_{days}_days"] + 1


def test_joining_horizons_are_nested_not_overlapping_buckets(client, make_candidate):
    """A candidate 20 days out belongs to the 30-day horizon only. If the
    horizons were exclusive buckets rather than cumulative windows the
    dashboard would read as though nobody joins next month."""
    before = _overview(client)
    make_candidate(
        offer_date=(TODAY - timedelta(days=10)).isoformat(),
        joining_date=(TODAY + timedelta(days=20)).isoformat(),
    )
    after = _overview(client)

    assert after["joining_next_7_days"] == before["joining_next_7_days"]
    assert after["joining_next_15_days"] == before["joining_next_15_days"]
    assert after["joining_next_30_days"] == before["joining_next_30_days"] + 1


def test_a_joining_date_that_has_passed_is_not_joining_soon(client, make_candidate):
    """Analytics is forward-looking. A pending candidate whose start date came
    and went is a serious problem, but they are not 'joining in the next 7
    days', and counting them there would inflate a number people plan
    headcount against."""
    before = _overview(client)
    make_candidate(
        offer_date=(TODAY - timedelta(days=70)).isoformat(),
        joining_date=(TODAY - timedelta(days=10)).isoformat(),
    )
    after = _overview(client)

    assert after["pending"] == before["pending"] + 1
    for days in (7, 15, 30):
        assert after[f"joining_next_{days}_days"] == before[f"joining_next_{days}_days"]


def test_the_same_overdue_candidate_is_still_caught_by_the_automation_rule(make_candidate):
    """The other half of the previous test, and the reason
    risk_service.joining_within takes an include_overdue flag instead of the
    two call sites each writing their own WHERE clause.

    Analytics excludes an overdue joiner from 'joining soon'; automation must
    still chase them. Same predicate, one explicit parameter apart — so the
    difference is a decision on the record rather than an accident.
    """
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=70)).isoformat(),
        joining_date=(TODAY - timedelta(days=10)).isoformat(),
    )
    candidate_id = uuid.UUID(candidate["id"])

    from app.services import risk_service

    with SessionLocal() as db:
        forward_only = db.scalars(
            select(Candidate.id).where(
                Candidate.id == candidate_id, risk_service.joining_within(TODAY, 7)
            )
        ).all()
        with_overdue = db.scalars(
            select(Candidate.id).where(
                Candidate.id == candidate_id,
                risk_service.joining_within(TODAY, 7, include_overdue=True),
            )
        ).all()

    assert forward_only == []
    assert with_overdue == [candidate_id]


def test_resolved_candidates_are_excluded_from_the_soon_and_risk_counts(
    client, make_candidate
):
    """A dropped-out candidate can still carry a future joining date, and
    risk_service never rescores resolved candidates, so a stale HIGH badge
    outlives them. Both would inflate the dashboard."""
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=10)).isoformat(),
        joining_date=(TODAY + timedelta(days=3)).isoformat(),
    )
    client.patch(
        f"{API}/candidates/{candidate['id']}",
        json={"risk_level": "high"},
        headers={"X-Actor": "hr-lead"},
    )
    with_pending = _overview(client)

    client.patch(f"{API}/candidates/{candidate['id']}", json={"final_outcome": "dropped_out"})
    after = _overview(client)

    assert after["pending"] == with_pending["pending"] - 1
    assert after["joining_next_7_days"] == with_pending["joining_next_7_days"] - 1
    assert after["high_risk_count"] == with_pending["high_risk_count"] - 1
    assert after["dropped_out"] == with_pending["dropped_out"] + 1


def test_conversion_counts_resolved_candidates_only(client, make_candidate):
    """A pending candidate must not move the rate. If they did, the number
    would sag every time an offer went out and recover as the cohort resolved
    — moving for reasons that have nothing to do with performance."""
    before = _overview(client)
    make_candidate()
    after_pending = _overview(client)

    assert after_pending["offer_to_join_conversion_pct"] == (
        before["offer_to_join_conversion_pct"]
    )

    joiner = make_candidate()
    client.patch(f"{API}/candidates/{joiner['id']}", json={"final_outcome": "joined"})
    after_join = _overview(client)

    expected = round(
        (before["joined"] + 1) * 100.0 / (before["joined"] + 1 + before["dropped_out"]), 1
    )
    assert after_join["offer_to_join_conversion_pct"] == expected


def test_avg_days_between_interactions_matches_an_independent_computation(client):
    """The endpoint's SQL against the same figure derived in plain Python.

    A Python loop is fine here — the "aggregation in SQL" rule is about the
    service, and recomputing by a different route is the only way to know the
    GROUP BY, the HAVING and the epoch arithmetic are all right.
    """
    with SessionLocal() as db:
        rows = db.execute(
            select(Interaction.candidate_id, Interaction.occurred_at)
            .join(Candidate, Candidate.id == Interaction.candidate_id)
            .where(Candidate.final_outcome == FinalOutcome.PENDING)
        ).all()

    by_candidate: dict[uuid.UUID, list] = {}
    for candidate_id, occurred_at in rows:
        by_candidate.setdefault(candidate_id, []).append(occurred_at)

    gaps = [
        (max(times) - min(times)).total_seconds() / 86400.0 / (len(times) - 1)
        for times in by_candidate.values()
        if len(times) > 1
    ]
    expected = round(sum(gaps) / len(gaps), 1)

    assert _overview(client)["avg_days_between_interactions"] == expected


def test_a_candidate_with_a_single_interaction_is_excluded_from_the_average(
    client, make_candidate
):
    """One interaction has no gap to measure. Counting it as a zero-day gap
    would drag the average toward zero for exactly the quiet candidates this
    product exists to notice."""
    before = _overview(client)["avg_days_between_interactions"]

    candidate = make_candidate()
    resp = client.post(
        f"{API}/candidates/{candidate['id']}/interactions",
        json={
            "channel": "email",
            "direction": "outbound",
            "content": "Welcome aboard.",
            "occurred_at": f"{TODAY.isoformat()}T09:00:00Z",
        },
    )
    assert resp.status_code == 201, resp.text

    assert _overview(client)["avg_days_between_interactions"] == before


def test_open_follow_up_actions_matches_the_queue(client):
    listed = client.get(f"{API}/follow-up-actions", params={"status": "open", "limit": 1}).json()
    assert _overview(client)["open_follow_up_actions"] == listed["total"]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def test_pipeline_returns_every_active_stage_in_sequence_order(client):
    items = _pipeline(client)["items"]

    assert len(items) == 8, "the seeded journey template is eight stages"
    assert [s["sequence_order"] for s in items] == list(range(1, 9))
    assert items[0]["stage_key"] == "offer_accepted"
    assert items[-1]["stage_key"] == "joining"


def test_every_stage_accounts_for_every_candidate(client):
    """candidate_stages is materialised for all 8 stages when a candidate is
    created, so each stage row must add up to the full population. A bad GROUP
    BY or a join that drops rows shows up here immediately, where a per-stage
    count in isolation would look plausible.

    Skipped stages come from a direct query because the endpoint does not
    expose them — they exist only on dropped-out candidates.
    """
    total = _overview(client)["total_offered"]

    with SessionLocal() as db:
        skipped_by_stage = dict(
            db.execute(
                select(JourneyStage.key, func.count(CandidateStage.id))
                .select_from(JourneyStage)
                .outerjoin(
                    CandidateStage,
                    (CandidateStage.stage_id == JourneyStage.id)
                    & (CandidateStage.status == StageStatus.SKIPPED),
                )
                .group_by(JourneyStage.key)
            ).all()
        )

    for stage in _pipeline(client)["items"]:
        accounted = stage["completed"] + stage["pending"] + skipped_by_stage[stage["stage_key"]]
        assert accounted == total, f"{stage['stage_key']} accounts for {accounted} of {total}"
        assert stage["stalled"] <= stage["pending"], "stalled is a subset of pending"


def test_drop_off_attributes_a_candidate_to_their_furthest_completed_stage(
    client, make_candidate
):
    """The definition, exercised end to end: complete three stages, drop out,
    and the count must land on the third — the last thing that went right —
    and on nothing else."""
    before = _pipeline(client)
    candidate = make_candidate()

    for key in ("offer_accepted", "welcome", "documentation"):
        _complete_stage(client, candidate["id"], key)
    client.patch(f"{API}/candidates/{candidate['id']}", json={"final_outcome": "dropped_out"})

    after = _pipeline(client)

    assert _stage(after, "documentation")["drop_off"] == _stage(before, "documentation")["drop_off"] + 1
    for key in ("offer_accepted", "welcome", "manager_intro"):
        assert _stage(after, key)["drop_off"] == _stage(before, key)["drop_off"], (
            f"{key} must not also claim this candidate"
        )
    assert after["total_dropped_out"] == before["total_dropped_out"] + 1


def test_a_pending_candidate_is_never_counted_as_drop_off(client, make_candidate):
    before = _pipeline(client)
    candidate = make_candidate()
    _complete_stage(client, candidate["id"], "offer_accepted")

    after = _pipeline(client)

    assert _stage(after, "offer_accepted")["drop_off"] == _stage(before, "offer_accepted")["drop_off"]
    assert _stage(after, "offer_accepted")["completed"] == (
        _stage(before, "offer_accepted")["completed"] + 1
    )


def test_drop_off_reconciles_with_the_dropped_out_total(client):
    """Every dropped-out candidate is counted exactly once — against a stage,
    or in the before-any-stage bucket. A mismatch means the attribution is
    double-counting or losing people."""
    body = _pipeline(client)

    attributed = sum(s["drop_off"] for s in body["items"])
    assert attributed + body["dropped_out_before_any_stage"] == body["total_dropped_out"]
    assert body["total_dropped_out"] == _overview(client)["dropped_out"]


def test_stalled_counts_agree_with_the_automation_rule(client):
    """The number on the dashboard and the actions in the queue come from the
    same predicate. If analytics said 11 stalls while the sweep filed 4, one of
    them would be lying and a recruiter would have no way to tell which."""
    with SessionLocal() as db:
        rule_stalls = len(automation_service._stalled_stages(db, TODAY))

    assert sum(s["stalled"] for s in _pipeline(client)["items"]) == rule_stalls


def test_a_stalled_stage_on_a_resolved_candidate_is_not_counted(client, make_candidate):
    """A stage left pending on someone who already dropped out is not
    actionable, and the automation rule ignores it for the same reason."""
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=30)).isoformat(),
        joining_date=(TODAY + timedelta(days=30)).isoformat(),
    )
    with_pending = sum(s["stalled"] for s in _pipeline(client)["items"])

    client.patch(f"{API}/candidates/{candidate['id']}", json={"final_outcome": "dropped_out"})
    after = sum(s["stalled"] for s in _pipeline(client)["items"])

    assert after < with_pending, "the candidate's overdue stages must drop out of the count"


# ---------------------------------------------------------------------------
# Recruiters
# ---------------------------------------------------------------------------


def test_recruiter_rows_reconcile_with_the_overview(client):
    rows = _recruiters(client)
    overview = _overview(client)

    assert len(rows) >= 6, "the seed creates six recruiters"
    assert sum(r["total_offers"] for r in rows) == overview["total_offered"]
    assert sum(r["joined"] for r in rows) == overview["joined"]
    assert sum(r["dropped_out"] for r in rows) == overview["dropped_out"]
    assert sum(r["pending_count"] for r in rows) == overview["pending"]
    assert sum(r["high_risk_count"] for r in rows) == overview["high_risk_count"]


def test_each_recruiters_own_numbers_are_internally_consistent(client):
    for row in _recruiters(client):
        assert row["joined"] + row["dropped_out"] + row["pending_count"] == row["total_offers"]
        assert row["high_risk_count"] <= row["pending_count"]
        assert row["conversion_pct"] == _conversion_pct(row["joined"], row["dropped_out"])


def test_a_new_candidate_lands_on_the_right_recruiter(client, make_candidate, temp_recruiter):
    recruiter_id = temp_recruiter()
    other_before = {r["recruiter_id"]: r["total_offers"] for r in _recruiters(client)}

    make_candidate(recruiter_id=recruiter_id)
    after = {r["recruiter_id"]: r for r in _recruiters(client)}

    assert after[recruiter_id]["total_offers"] == 1
    for rid, count in other_before.items():
        if rid != recruiter_id:
            assert after[rid]["total_offers"] == count


def test_avg_days_since_last_contact_uses_the_offer_date_when_never_contacted(
    client, make_candidate, temp_recruiter
):
    """Same rule as risk_service.days_since_contact: the silence clock starts
    when the offer was made, not when someone first bothered to call. A null
    here would hide the worst candidates from the staleness metric."""
    recruiter_id = temp_recruiter()
    make_candidate(
        recruiter_id=recruiter_id,
        offer_date=(TODAY - timedelta(days=12)).isoformat(),
        joining_date=(TODAY + timedelta(days=48)).isoformat(),
    )

    row = next(r for r in _recruiters(client) if r["recruiter_id"] == recruiter_id)

    assert row["avg_days_since_last_contact"] == 12.0


def test_avg_days_since_last_contact_ignores_resolved_candidates(
    client, make_candidate, temp_recruiter
):
    """The question is how stale the LIVE pipeline is. A candidate who joined
    months ago would drag it toward whatever the gap happened to be then."""
    recruiter_id = temp_recruiter()
    make_candidate(
        recruiter_id=recruiter_id,
        offer_date=(TODAY - timedelta(days=4)).isoformat(),
        joining_date=(TODAY + timedelta(days=56)).isoformat(),
    )
    stale = make_candidate(
        recruiter_id=recruiter_id,
        offer_date=(TODAY - timedelta(days=90)).isoformat(),
        joining_date=(TODAY - timedelta(days=30)).isoformat(),
    )
    client.patch(f"{API}/candidates/{stale['id']}", json={"final_outcome": "joined"})

    row = next(r for r in _recruiters(client) if r["recruiter_id"] == recruiter_id)

    assert row["avg_days_since_last_contact"] == 4.0, "only the pending candidate counts"
    assert row["pending_count"] == 1
    assert row["joined"] == 1


# ---------------------------------------------------------------------------
# Enum binding — the failure mode that has bitten three times
# ---------------------------------------------------------------------------


def test_every_analytics_endpoint_survives_its_enum_comparisons(client):
    """Postgres stores these enums by their Python NAME ('HIGH', 'DROPPED_OUT')
    while the values are lowercase, so a raw string literal in a filter fails
    with `invalid input value for enum`. All three endpoints filter on enums;
    a 200 from each is the cheapest proof that every comparison goes through
    the column type rather than binding a literal.
    """
    for path in ("overview", "pipeline", "recruiters"):
        resp = client.get(f"{API}/analytics/{path}")
        assert resp.status_code == 200, f"{path}: {resp.text}"


def test_risk_counts_use_the_stored_band(client, make_candidate):
    """high_risk_count must track the same column the badge renders, including
    an HR override — not a recomputed floor."""
    before = _overview(client)
    candidate = make_candidate()
    client.patch(
        f"{API}/candidates/{candidate['id']}",
        json={"risk_level": "high"},
        headers={"X-Actor": "hr-lead"},
    )

    after = _overview(client)
    assert after["high_risk_count"] == before["high_risk_count"] + 1

    with SessionLocal() as db:
        assert db.get(Candidate, uuid.UUID(candidate["id"])).risk_level == RiskLevel.HIGH
