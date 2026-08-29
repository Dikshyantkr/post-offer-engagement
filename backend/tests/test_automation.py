"""Module 6 automation tests.

Every AI call goes through the shared FakeProvider in tests/helpers.py —
nothing here touches real Gemini.

The sweep is exercised through POST /automation/run rather than by calling
`run_engagement_sweep` directly, because the demo path and the scheduled path
are the same function and the endpoint is the one a reviewer will press.

Two things get the most attention:

* the rule boundaries. `joining_date <= today + 7` and `silent >= 5 days` are
  the whole of rule 1, and an off-by-one in either direction either floods the
  queue or misses the candidate the system exists to catch.
* graceful degradation. A silent candidate must be flagged whether or not the
  provider answered, so every failure mode still has to produce an action.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.enums import FollowUpPriority, FollowUpSource, FollowUpStatus, StageStatus
from app.models import AIAnalysis, AuditLog, Candidate, CandidateStage, FollowUpAction
from app.services import automation_service
from tests.helpers import API, draft_json, risk_json

TODAY = date.today()

# The sweep makes two AI calls per rule-1 candidate: assess, then draft.
AI_CALLS_PER_SILENT_CANDIDATE = 2


# ---------------------------------------------------------------------------
# Fixtures
#
# The seeded 54 include candidates that legitimately match both rules, so a
# sweep over the whole database would create actions for them and trip the
# row-leak guard. Every test here therefore runs the sweep against a database
# temporarily narrowed to its own candidate: `only_candidate` parks every
# other candidate's matching state out of reach for the duration of the test
# and puts it back afterwards.
# ---------------------------------------------------------------------------


@pytest.fixture
def sweep(client, use_provider):
    """Run the sweep through the API with a queue of fake AI responses.

    Rule 1 needs two responses per candidate it catches — a risk assessment
    and a drafted message.
    """

    def _sweep(*responses, actor: str = "test-automation") -> dict:
        if responses:
            use_provider(*responses)
        else:
            use_provider()
        resp = client.post(f"{API}/automation/run", headers={"X-Actor": actor})
        assert resp.status_code == 200, resp.text
        return resp.json()

    return _sweep


@pytest.fixture
def only_candidate(client):
    """Narrow the sweep's view to one candidate.

    Nothing here mutates another candidate's real data. Rule 1 is neutralised
    for everyone else by moving `last_interaction_at` to now, and rule 2 by
    moving open overdue due dates into the future; both are restored exactly
    at teardown. This keeps the tests deterministic without needing a separate
    empty database, and the conftest row-leak guard still proves nothing was
    left behind.
    """
    saved_contact: list[tuple[uuid.UUID, datetime | None]] = []
    saved_due: list[tuple[uuid.UUID, date]] = []

    def _only(candidate_id: str):
        keep = uuid.UUID(candidate_id)
        now = datetime.now(timezone.utc)
        far_future = TODAY + timedelta(days=3650)

        with SessionLocal() as db:
            for other in db.scalars(select(Candidate).where(Candidate.id != keep)):
                saved_contact.append((other.id, other.last_interaction_at))
                other.last_interaction_at = now

            stalled = db.scalars(
                select(CandidateStage).where(
                    CandidateStage.candidate_id != keep,
                    CandidateStage.status == StageStatus.PENDING,
                    CandidateStage.due_date < TODAY,
                )
            )
            for candidate_stage in stalled:
                saved_due.append((candidate_stage.id, candidate_stage.due_date))
                candidate_stage.due_date = far_future

            db.commit()

    yield _only

    with SessionLocal() as db:
        for candidate_id, value in saved_contact:
            db.get(Candidate, candidate_id).last_interaction_at = value
        for stage_id, value in saved_due:
            db.get(CandidateStage, stage_id).due_date = value
        db.commit()


def _set_last_contact(candidate_id: str, days_ago: int | None) -> None:
    with SessionLocal() as db:
        candidate = db.get(Candidate, uuid.UUID(candidate_id))
        candidate.last_interaction_at = (
            None
            if days_ago is None
            else datetime.now(timezone.utc) - timedelta(days=days_ago, hours=1)
        )
        db.commit()


def _clear_stage_stalls(candidate_id: str) -> None:
    """Push this candidate's overdue stages out of rule 2's way, so a rule-1
    test measures rule 1 only."""
    with SessionLocal() as db:
        for candidate_stage in db.scalars(
            select(CandidateStage).where(
                CandidateStage.candidate_id == uuid.UUID(candidate_id),
                CandidateStage.status == StageStatus.PENDING,
            )
        ):
            candidate_stage.due_date = TODAY + timedelta(days=3650)
        db.commit()


def _actions(candidate_id: str, rule_key: str | None = None) -> list[FollowUpAction]:
    with SessionLocal() as db:
        stmt = select(FollowUpAction).where(
            FollowUpAction.candidate_id == uuid.UUID(candidate_id)
        )
        if rule_key is not None:
            stmt = stmt.where(FollowUpAction.rule_key == rule_key)
        return list(db.scalars(stmt.order_by(FollowUpAction.created_at)))


def _silent_joiner(make_candidate, only_candidate, *, joining_in: int, silent_days: int | None):
    """A candidate rule 1 should catch: joining soon, out of contact."""
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=60)).isoformat(),
        joining_date=(TODAY + timedelta(days=joining_in)).isoformat(),
    )
    _set_last_contact(candidate["id"], silent_days)
    _clear_stage_stalls(candidate["id"])
    only_candidate(candidate["id"])
    return candidate


# ---------------------------------------------------------------------------
# Rule 1 — imminent_silence
# ---------------------------------------------------------------------------


def test_rule_1_catches_a_candidate_joining_in_5_days_silent_for_6(
    make_candidate, only_candidate, sweep
):
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)

    summary = sweep(risk_json("high"), draft_json())

    assert summary["rules"]["imminent_silence"]["matched"] == 1
    assert summary["rules"]["imminent_silence"]["actions_created"] == 1
    assert summary["ai_calls"] == AI_CALLS_PER_SILENT_CANDIDATE
    assert summary["ai_fallbacks"] == 0
    assert summary["messages_simulated"] == 1

    actions = _actions(candidate["id"], "imminent_silence")
    assert len(actions) == 1
    action = actions[0]
    assert action.source == FollowUpSource.AUTOMATION
    assert action.status == FollowUpStatus.OPEN
    assert action.rule_key == "imminent_silence"
    assert action.generated_message, "the drafted message must be stored on the action"
    assert "Checking in before your start date" in action.generated_message
    assert candidate["name"] in action.title
    assert "silent 6 days" in action.title


def test_rule_1_ignores_a_candidate_joining_in_5_days_contacted_yesterday(
    make_candidate, only_candidate, sweep
):
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=1)

    summary = sweep()

    assert summary["rules"]["imminent_silence"]["matched"] == 0
    assert summary["ai_calls"] == 0, "no provider call for a candidate the rule did not match"
    assert _actions(candidate["id"]) == []


def test_rule_1_ignores_a_candidate_joining_in_30_days_even_if_silent(
    make_candidate, only_candidate, sweep
):
    """Silence alone is not this rule. Joining far out and quiet is the risk
    engine's problem, not an urgent call today."""
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=30, silent_days=20)

    summary = sweep()

    assert summary["rules"]["imminent_silence"]["matched"] == 0
    assert _actions(candidate["id"]) == []


def test_rule_1_ignores_a_joined_candidate(client, make_candidate, only_candidate, sweep):
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)
    client.patch(f"{API}/candidates/{candidate['id']}", json={"final_outcome": "joined"})

    summary = sweep()

    assert summary["rules"]["imminent_silence"]["matched"] == 0
    assert _actions(candidate["id"]) == []


def test_rule_1_ignores_a_dropped_out_candidate(client, make_candidate, only_candidate, sweep):
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)
    client.patch(f"{API}/candidates/{candidate['id']}", json={"final_outcome": "dropped_out"})

    summary = sweep()

    assert summary["rules"]["imminent_silence"]["matched"] == 0
    assert _actions(candidate["id"]) == []


def test_rule_1_catches_a_never_contacted_candidate(make_candidate, only_candidate, sweep):
    """last_interaction_at IS NULL is the worst case, not a missing value to
    skip over — nobody has ever spoken to them and they join next week."""
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=None)

    summary = sweep(risk_json("high"), draft_json())

    assert summary["rules"]["imminent_silence"]["actions_created"] == 1
    action = _actions(candidate["id"], "imminent_silence")[0]
    # Silence for a never-contacted candidate counts from the offer date, the
    # same way risk_service counts it.
    assert "silent 60 days" in action.title


def test_rule_1_catches_a_candidate_whose_joining_date_has_passed(
    make_candidate, only_candidate, sweep
):
    """`joining_date <= now + 7d` has no lower bound, deliberately. A pending
    candidate whose start date came and went is the most alarming row in the
    table and must not fall out of the query."""
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=-3, silent_days=10)

    summary = sweep(risk_json("high"), draft_json())

    assert summary["rules"]["imminent_silence"]["actions_created"] == 1
    assert "joining in -3 days" in _actions(candidate["id"], "imminent_silence")[0].title


@pytest.mark.parametrize(
    "joining_in,silent_days,expected",
    [
        (7, 5, 1),  # both thresholds exactly met — inclusive
        (8, 5, 0),  # one day past the joining horizon
        (7, 4, 0),  # one day short of the silence threshold
    ],
)
def test_rule_1_boundaries_are_inclusive_on_both_thresholds(
    make_candidate, only_candidate, sweep, joining_in, silent_days, expected
):
    candidate = _silent_joiner(
        make_candidate, only_candidate, joining_in=joining_in, silent_days=silent_days
    )

    summary = sweep(risk_json("high"), draft_json())

    assert summary["rules"]["imminent_silence"]["actions_created"] == expected
    assert len(_actions(candidate["id"], "imminent_silence")) == expected


def test_rule_1_priority_follows_the_resulting_risk_level(
    make_candidate, only_candidate, sweep
):
    """Everything rule 1 catches is silent AND about to join, which the risk
    rules already band as HIGH — so urgent is the expected outcome here."""
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)

    sweep(risk_json("high"), draft_json())

    assert _actions(candidate["id"], "imminent_silence")[0].priority == FollowUpPriority.URGENT


def test_an_hr_override_is_respected_in_the_action_priority(
    client, make_candidate, only_candidate, sweep
):
    """A human who has already looked at this candidate and called them low
    risk should not be shouted at by the queue."""
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)
    client.patch(
        f"{API}/candidates/{candidate['id']}",
        json={"risk_level": "low"},
        headers={"X-Actor": "hr-lead"},
    )

    sweep(risk_json("high"), draft_json())

    action = _actions(candidate["id"], "imminent_silence")[0]
    assert action.priority == FollowUpPriority.MEDIUM
    # The action still exists: they are still silent and still joining.
    assert action.status == FollowUpStatus.OPEN


def test_the_action_description_carries_the_ai_evidence(make_candidate, only_candidate, sweep):
    """The queue is where a recruiter decides what to do first. An action that
    says only 'flagged' makes them open the candidate to find out why."""
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)

    sweep(risk_json("high"), draft_json())

    description = _actions(candidate["id"], "imminent_silence")[0].description
    assert "no contact for 6 days" in description
    assert "Current employer initiated a conversation" in description, "the AI reasoning"
    assert "called me in for a chat tomorrow" in description, "the quoted evidence"


def test_the_sweep_runs_the_risk_assessment_and_can_raise_risk(
    make_candidate, only_candidate, sweep
):
    """CLAUDE.md: rule 1 is 'raise risk, run assessment, draft message'. The
    assessment is persisted like any other, so the candidate detail shows it."""
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)

    sweep(risk_json("high"), draft_json())

    with SessionLocal() as db:
        analyses = list(
            db.scalars(
                select(AIAnalysis).where(AIAnalysis.candidate_id == uuid.UUID(candidate["id"]))
            )
        )
    types = sorted(a.analysis_type for a in analyses)
    assert types == ["drafted_message", "risk_assessment"]


def test_the_drafted_message_uses_the_channel_the_candidate_last_replied_on(
    client, make_candidate, only_candidate, sweep
):
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=60)).isoformat(),
        joining_date=(TODAY + timedelta(days=5)).isoformat(),
    )
    client.post(
        f"{API}/candidates/{candidate['id']}/interactions",
        json={
            "channel": "whatsapp",
            "direction": "inbound",
            "content": "will get back to you",
            "occurred_at": f"{(TODAY - timedelta(days=6)).isoformat()}T10:00:00Z",
        },
    )
    _clear_stage_stalls(candidate["id"])
    only_candidate(candidate["id"])

    provider_responses = (
        risk_json("high"),
        draft_json(channel="whatsapp", subject=None, body="hey, how's the handover going?"),
    )
    sweep(*provider_responses)

    action = _actions(candidate["id"], "imminent_silence")[0]
    assert action.generated_message == "hey, how's the handover going?"
    assert "Subject:" not in action.generated_message


# ---------------------------------------------------------------------------
# Rule 2 — stage_stall
# ---------------------------------------------------------------------------


def _stall_one_stage(candidate_id: str, days_overdue: int) -> str:
    """Leave exactly one pending stage overdue and return its label."""
    with SessionLocal() as db:
        stages = list(
            db.scalars(
                select(CandidateStage)
                .where(CandidateStage.candidate_id == uuid.UUID(candidate_id))
                .order_by(CandidateStage.due_date)
            )
        )
        for candidate_stage in stages:
            candidate_stage.status = StageStatus.COMPLETED

        target = stages[0]
        target.status = StageStatus.PENDING
        target.due_date = TODAY - timedelta(days=days_overdue)
        label = target.stage.label
        db.commit()
    return label


def _stalled_candidate(make_candidate, only_candidate, *, days_overdue: int = 10):
    """Joining far out and freshly contacted, so only rule 2 can fire."""
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=30)).isoformat(),
        joining_date=(TODAY + timedelta(days=60)).isoformat(),
    )
    _set_last_contact(candidate["id"], 0)
    label = _stall_one_stage(candidate["id"], days_overdue)
    only_candidate(candidate["id"])
    return candidate, label


def test_rule_2_catches_an_overdue_pending_stage(make_candidate, only_candidate, sweep):
    candidate, label = _stalled_candidate(make_candidate, only_candidate, days_overdue=10)

    summary = sweep()

    assert summary["rules"]["stage_stall"]["matched"] == 1
    assert summary["rules"]["stage_stall"]["actions_created"] == 1
    assert summary["ai_calls"] == 0, "rule 2 is deterministic — it must not call the model"

    action = _actions(candidate["id"], "stage_stall")[0]
    assert label in action.title, "the action must name the stalled stage"
    assert "10 days" in action.title
    assert action.source == FollowUpSource.AUTOMATION
    assert action.generated_message is None, "nothing to send — the company is the one that is late"


def test_rule_2_ignores_a_completed_stage_that_was_finished_late(
    make_candidate, only_candidate, sweep
):
    """A stage completed after its due date is not a stall. It was dealt with."""
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=30)).isoformat(),
        joining_date=(TODAY + timedelta(days=60)).isoformat(),
    )
    _set_last_contact(candidate["id"], 0)
    with SessionLocal() as db:
        for candidate_stage in db.scalars(
            select(CandidateStage).where(
                CandidateStage.candidate_id == uuid.UUID(candidate["id"])
            )
        ):
            candidate_stage.status = StageStatus.COMPLETED
            candidate_stage.due_date = TODAY - timedelta(days=10)
        db.commit()
    only_candidate(candidate["id"])

    summary = sweep()

    assert summary["rules"]["stage_stall"]["matched"] == 0
    assert _actions(candidate["id"]) == []


def test_rule_2_ignores_a_pending_stage_that_is_not_yet_due(
    make_candidate, only_candidate, sweep
):
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=30)).isoformat(),
        joining_date=(TODAY + timedelta(days=60)).isoformat(),
    )
    _set_last_contact(candidate["id"], 0)
    _clear_stage_stalls(candidate["id"])
    only_candidate(candidate["id"])

    summary = sweep()

    assert summary["rules"]["stage_stall"]["matched"] == 0


def test_rule_2_ignores_a_joined_candidate(client, make_candidate, only_candidate, sweep):
    candidate, _label = _stalled_candidate(make_candidate, only_candidate)
    client.patch(f"{API}/candidates/{candidate['id']}", json={"final_outcome": "joined"})

    summary = sweep()

    assert summary["rules"]["stage_stall"]["matched"] == 0
    assert _actions(candidate["id"]) == []


def test_a_candidate_with_several_overdue_stages_gets_one_action_naming_the_worst(
    make_candidate, only_candidate, sweep
):
    """The idempotency key is candidate + rule_key, so several stalls can only
    ever produce one action. It names the most overdue stage — the earliest
    one, which is also the one blocking the rest — and lists the others."""
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=40)).isoformat(),
        joining_date=(TODAY + timedelta(days=60)).isoformat(),
    )
    _set_last_contact(candidate["id"], 0)
    with SessionLocal() as db:
        stages = list(
            db.scalars(
                select(CandidateStage)
                .where(CandidateStage.candidate_id == uuid.UUID(candidate["id"]))
                .order_by(CandidateStage.due_date)
            )
        )
        for candidate_stage in stages:
            candidate_stage.status = StageStatus.COMPLETED
        for offset, candidate_stage in enumerate(stages[:3]):
            candidate_stage.status = StageStatus.PENDING
            candidate_stage.due_date = TODAY - timedelta(days=20 - offset * 5)
        worst_label = stages[0].stage.label
        db.commit()
    only_candidate(candidate["id"])

    summary = sweep()

    assert summary["rules"]["stage_stall"]["matched"] == 1, "one candidate, not three stages"
    actions = _actions(candidate["id"], "stage_stall")
    assert len(actions) == 1
    assert worst_label in actions[0].title
    assert "3 stages are overdue" in actions[0].description


def test_rule_2_priority_escalates_when_the_joining_date_is_close(
    make_candidate, only_candidate, sweep
):
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=60)).isoformat(),
        joining_date=(TODAY + timedelta(days=5)).isoformat(),
    )
    _set_last_contact(candidate["id"], 0)
    _stall_one_stage(candidate["id"], 10)
    only_candidate(candidate["id"])

    sweep()

    assert _actions(candidate["id"], "stage_stall")[0].priority == FollowUpPriority.URGENT


def test_a_badly_overdue_stage_outranks_a_fresh_one_even_when_joining_is_far_off(
    make_candidate, only_candidate, sweep
):
    """The queue answers "what do I fix today?", not "will they join?". A step
    nobody has touched in three weeks has to sort above one that slipped
    yesterday, or the priority column stops distinguishing them at all."""
    stale = make_candidate(
        offer_date=(TODAY - timedelta(days=60)).isoformat(),
        joining_date=(TODAY + timedelta(days=40)).isoformat(),
    )
    _set_last_contact(stale["id"], 0)
    _stall_one_stage(stale["id"], 22)
    only_candidate(stale["id"])

    sweep()

    assert _actions(stale["id"], "stage_stall")[0].priority == FollowUpPriority.HIGH


def test_a_freshly_overdue_stage_with_a_distant_joining_date_stays_medium(
    make_candidate, only_candidate, sweep
):
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=60)).isoformat(),
        joining_date=(TODAY + timedelta(days=40)).isoformat(),
    )
    _set_last_contact(candidate["id"], 0)
    _stall_one_stage(candidate["id"], 1)
    only_candidate(candidate["id"])

    sweep()

    assert _actions(candidate["id"], "stage_stall")[0].priority == FollowUpPriority.MEDIUM


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_running_the_sweep_twice_creates_no_duplicate_actions(
    make_candidate, only_candidate, sweep
):
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)

    first = sweep(risk_json("high"), draft_json())
    assert first["rules"]["imminent_silence"]["actions_created"] == 1
    after_first = len(_actions(candidate["id"]))

    # No responses queued for the second run: if it tried to call the model,
    # the FakeProvider would raise and fail this test.
    second = sweep()

    assert second["rules"]["imminent_silence"]["matched"] == 1, "it still matches the rule"
    assert second["rules"]["imminent_silence"]["actions_created"] == 0
    assert second["rules"]["imminent_silence"]["skipped_existing_action"] == 1
    assert second["ai_calls"] == 0, "the guard runs before the AI call, so a repeat costs nothing"
    assert len(_actions(candidate["id"])) == after_first


def test_running_the_stage_stall_sweep_twice_creates_no_duplicate_actions(
    make_candidate, only_candidate, sweep
):
    candidate, _label = _stalled_candidate(make_candidate, only_candidate)

    sweep()
    after_first = len(_actions(candidate["id"], "stage_stall"))
    second = sweep()

    assert after_first == 1
    assert second["rules"]["stage_stall"]["actions_created"] == 0
    assert second["rules"]["stage_stall"]["skipped_existing_action"] == 1
    assert len(_actions(candidate["id"], "stage_stall")) == 1


def test_a_completed_action_does_not_block_a_new_one(make_candidate, only_candidate, sweep):
    """The guard is about OPEN actions. Once a recruiter has made the call and
    closed it, the candidate being silent again tomorrow is new information."""
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)
    sweep(risk_json("high"), draft_json())

    action = _actions(candidate["id"], "imminent_silence")[0]
    with SessionLocal() as db:
        db.get(FollowUpAction, action.id).status = FollowUpStatus.DONE
        db.commit()

    second = sweep(risk_json("high"), draft_json())

    assert second["rules"]["imminent_silence"]["actions_created"] == 1
    assert len(_actions(candidate["id"], "imminent_silence")) == 2


def test_an_open_action_older_than_the_window_does_not_block_a_new_one(
    make_candidate, only_candidate, sweep
):
    """The window is 24h, so a candidate still silent tomorrow is raised
    again. That is escalation, not duplication — the first nudge went
    unactioned and their situation is now a day worse."""
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)
    sweep(risk_json("high"), draft_json())

    action = _actions(candidate["id"], "imminent_silence")[0]
    with SessionLocal() as db:
        db.get(FollowUpAction, action.id).created_at = datetime.now(timezone.utc) - timedelta(
            hours=automation_service.IDEMPOTENCY_WINDOW_HOURS + 1
        )
        db.commit()

    second = sweep(risk_json("high"), draft_json())

    assert second["rules"]["imminent_silence"]["actions_created"] == 1
    assert len(_actions(candidate["id"], "imminent_silence")) == 2


def test_the_two_rules_do_not_block_each_other(make_candidate, only_candidate, sweep):
    """Different rule_keys, so a candidate who is both silent and stalled gets
    one action from each."""
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=60)).isoformat(),
        joining_date=(TODAY + timedelta(days=5)).isoformat(),
    )
    _set_last_contact(candidate["id"], 6)
    _stall_one_stage(candidate["id"], 10)
    only_candidate(candidate["id"])

    summary = sweep(risk_json("high"), draft_json())

    assert summary["rules"]["imminent_silence"]["actions_created"] == 1
    assert summary["rules"]["stage_stall"]["actions_created"] == 1
    assert {a.rule_key for a in _actions(candidate["id"])} == {
        "imminent_silence",
        "stage_stall",
    }


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


def test_provider_failure_still_produces_an_action_with_a_fallback_message(
    make_candidate, only_candidate, sweep
):
    """The point of the whole module. A candidate five days from joining who
    has gone quiet is the single most important thing this system detects, and
    detecting them must not depend on a third-party API being up."""
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)

    summary = sweep(
        RuntimeError("gemini: 429 rate limited"),
        RuntimeError("gemini: 429 rate limited"),
    )

    assert summary["rules"]["imminent_silence"]["actions_created"] == 1
    assert summary["ai_fallbacks"] == AI_CALLS_PER_SILENT_CANDIDATE
    assert summary["errors"] == 0, "a provider outage is handled, not an error"

    action = _actions(candidate["id"], "imminent_silence")[0]
    assert action.generated_message, "a fallback message, not an empty one"
    assert action.priority == FollowUpPriority.URGENT
    assert "Rule-based fallback" in action.description


def test_an_ai_layer_that_raises_outright_still_produces_an_action(
    monkeypatch, make_candidate, only_candidate, sweep
):
    """The previous test exercises the engine degrading, which is the engine's
    own guarantee. This one breaks the layer above it — the case
    `_template_message` exists for. Without it a bug anywhere in ai_service
    would silently drop every silent candidate on the floor, which is the one
    failure this system cannot have."""
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)

    def _boom(*args, **kwargs):
        raise RuntimeError("ai_service is broken")

    monkeypatch.setattr(automation_service.ai_service, "assess_risk", _boom)

    summary = sweep()

    assert summary["rules"]["imminent_silence"]["actions_created"] == 1
    assert summary["errors"] == 1, "the failure is recorded, not swallowed"

    action = _actions(candidate["id"], "imminent_silence")[0]
    assert action.priority == FollowUpPriority.URGENT
    assert "Just checking in ahead of your start date" in action.generated_message, (
        "the deterministic template, written in automation_service so it does not "
        "depend on the module that just broke"
    )
    assert "AI layer was unavailable" in action.description
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(AIAnalysis).where(
                    AIAnalysis.candidate_id == uuid.UUID(candidate["id"])
                )
            )
            is None
        ), "no analysis was produced, and the action does not pretend otherwise"


def test_a_malformed_then_repaired_response_still_produces_one_action(
    make_candidate, only_candidate, sweep
):
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)

    summary = sweep("{ not json", risk_json("high"), draft_json())

    assert summary["rules"]["imminent_silence"]["actions_created"] == 1
    assert summary["ai_fallbacks"] == 0, "a repaired response is not a fallback"
    assert _actions(candidate["id"], "imminent_silence")[0].generated_message


def test_one_candidate_failing_does_not_stop_the_sweep(
    monkeypatch, make_candidate, only_candidate, sweep
):
    """Per-candidate commits mean a blow-up costs one candidate, not the run.
    The stage_stall rule still has to complete."""
    candidate = make_candidate(
        offer_date=(TODAY - timedelta(days=60)).isoformat(),
        joining_date=(TODAY + timedelta(days=5)).isoformat(),
    )
    _set_last_contact(candidate["id"], 6)
    _stall_one_stage(candidate["id"], 10)
    only_candidate(candidate["id"])

    def _explode(*args, **kwargs):
        raise RuntimeError("database went away mid-action")

    monkeypatch.setattr(automation_service, "_record_action", _explode)

    summary = sweep(risk_json("high"), draft_json())

    assert summary["errors"] == 2, "both rules failed for this candidate, both were caught"
    assert summary["actions_created"] == 0
    # And the endpoint still returned a summary rather than a 500.


# ---------------------------------------------------------------------------
# Audit + endpoint shape
# ---------------------------------------------------------------------------


def test_every_created_action_is_audited(make_candidate, only_candidate, sweep):
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)

    sweep(risk_json("high"), draft_json(), actor="meera.iyer")

    action = _actions(candidate["id"], "imminent_silence")[0]
    with SessionLocal() as db:
        row = db.scalar(
            select(AuditLog).where(
                AuditLog.entity_id == action.id,
                AuditLog.action == "automation_action_created",
            )
        )
    assert row is not None
    assert row.entity_type == "follow_up_action"
    assert row.actor == "meera.iyer"
    assert row.after["rule_key"] == "imminent_silence"
    assert row.after["days_silent"] == 6
    assert row.after["ai_available"] is True


def test_the_run_summary_has_the_shape_the_demo_needs(make_candidate, only_candidate, sweep):
    _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)

    summary = sweep(risk_json("high"), draft_json())

    assert set(summary) == {
        "started_at",
        "duration_ms",
        "candidates_scanned",
        "actions_created",
        "rules",
        "ai_calls",
        "ai_fallbacks",
        "messages_simulated",
        "errors",
    }
    assert set(summary["rules"]) == {"imminent_silence", "stage_stall"}
    assert summary["candidates_scanned"] >= 1, "counts every pending candidate considered"
    assert summary["duration_ms"] >= 0


def test_created_actions_show_up_in_the_action_queue(client, make_candidate, only_candidate, sweep):
    """Module 7's action queue reads GET /follow-up-actions. This is the first
    module that puts anything in it."""
    candidate = _silent_joiner(make_candidate, only_candidate, joining_in=5, silent_days=6)
    sweep(risk_json("high"), draft_json())

    resp = client.get(f"{API}/follow-up-actions", params={"status": "open", "limit": 100})

    assert resp.status_code == 200
    mine = [a for a in resp.json()["items"] if a["candidate_id"] == candidate["id"]]
    assert len(mine) == 1
    assert mine[0]["source"] == "automation"
    assert mine[0]["rule_key"] == "imminent_silence"
    assert mine[0]["generated_message"]


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


def test_the_scheduler_is_disabled_during_tests():
    """The guard that keeps a background thread from firing real sweeps —
    and real provider calls — in the middle of a test run."""
    from app.config import settings

    assert settings.run_scheduler is False


def test_the_scheduler_registers_a_daily_cron_job_without_starting_it():
    from app.scheduler import SWEEP_JOB_ID, build_scheduler

    scheduler = build_scheduler()
    try:
        job = scheduler.get_job(SWEEP_JOB_ID)
        assert job is not None
        assert job.max_instances == 1, "two concurrent sweeps would race the idempotency check"
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields["hour"] == "2"
        assert fields["minute"] == "0"
        assert fields["day"] == "*", "daily"
    finally:
        assert not scheduler.running, "build_scheduler must not start anything"


def test_start_scheduler_returns_none_when_disabled():
    from app.scheduler import start_scheduler

    assert start_scheduler() is None
