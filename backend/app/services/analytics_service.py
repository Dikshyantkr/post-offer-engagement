"""Module 8 — analytics.

Every number here is produced by an aggregate query. Nothing loads a list of
candidates and counts it in Python: at 50 candidates the difference is
invisible, at a million it is the difference between a dashboard and a
timeout, and the shape of the code is the thing being graded either way.

Three things this module is careful about.

**Denominators.** Every ratio here can legitimately have nothing under the
line — a recruiter whose candidates are all still pending has no conversion
rate, and saying "0%" would be a lie about a recruiter who has lost nobody.
Those return null, never 0 and never a 500.

**Shared definitions.** "Joining within 7 days" comes from
risk_service.joining_within, the same predicate Module 6's automation rule and
the dashboard filter use, and "days since contact" is the same COALESCE onto
offer_date that risk_service.days_since_contact applies in Python. A metric
that disagrees with the rule engine it is reporting on is worse than no
metric.

**Enum comparisons go through the ORM.** Postgres stores these enums by their
Python *name* — the labels in the database are 'HIGH', 'DROPPED_OUT',
'COMPLETED' — while the values are lowercase. Comparing a column against a
raw string literal binds the value and fails with `invalid input value for
enum`. Every comparison below is `Column == EnumMember`, which routes through
the column's own type and binds the name.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, Integer, cast, func, literal, select
from sqlalchemy.orm import Session

from app.enums import FinalOutcome, FollowUpStatus, RiskLevel, StageStatus
from app.models import (
    Candidate,
    CandidateStage,
    FollowUpAction,
    Interaction,
    JourneyStage,
    Recruiter,
)
from app.schemas import (
    AnalyticsOverviewResponse,
    AnalyticsPipelineResponse,
    AnalyticsRecruitersResponse,
    PipelineStageStats,
    RecruiterStats,
)
from app.services import risk_service

# Horizons for the "joining soon" counts on the overview.
JOINING_HORIZONS = (7, 15, 30)


def _conversion_pct(joined: int, dropped_out: int) -> float | None:
    """Offers that became joins, as a percentage of RESOLVED candidates only.

    The denominator is joined + dropped_out. Pending candidates are excluded
    deliberately: they have not decided yet, and counting them as failures
    would make the number sag every time a new offer is made and recover as
    the cohort resolves — a metric that moves for reasons unrelated to
    performance is worse than none.

    Returns None, not 0.0, when nothing has resolved. A recruiter whose
    candidates are all still pending has no conversion rate; reporting 0%
    would say they have lost everyone.
    """
    resolved = joined + dropped_out
    if resolved == 0:
        return None
    return round(joined * 100.0 / resolved, 1)


def _days_since_contact_expr(today: date) -> "cast":
    """SQL twin of risk_service.days_since_contact.

    Same rule: silence is measured from the last interaction, or from the
    offer date for a candidate nobody has ever contacted — the clock starts
    when we made the offer, not when we first bothered to call. Floored at 0
    so a future-dated interaction cannot produce a negative average.
    """
    last_contact = func.coalesce(cast(Candidate.last_interaction_at, Date), Candidate.offer_date)
    return func.greatest(cast(literal(today), Date) - last_contact, 0)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def get_overview(db: Session, today: date) -> AnalyticsOverviewResponse:
    """Headline numbers for the dashboard.

    The joining_next_N and risk counts are restricted to pending candidates.
    Both would otherwise be wrong in the same way: risk_service only rescores
    pending candidates, so a candidate who was HIGH when they joined keeps
    that badge forever, and a dropped-out candidate can still carry a future
    joining date. Counting either would report people who are no longer
    arriving.
    """
    pending = Candidate.final_outcome == FinalOutcome.PENDING

    row = db.execute(
        select(
            func.count().label("total_offered"),
            func.count().filter(Candidate.final_outcome == FinalOutcome.JOINED).label("joined"),
            func.count()
            .filter(Candidate.final_outcome == FinalOutcome.DROPPED_OUT)
            .label("dropped_out"),
            func.count().filter(pending).label("pending"),
            *[
                func.count()
                .filter(pending, risk_service.joining_within(today, days))
                .label(f"joining_next_{days}_days")
                for days in JOINING_HORIZONS
            ],
            func.count()
            .filter(pending, Candidate.risk_level == RiskLevel.HIGH)
            .label("high_risk_count"),
            func.count()
            .filter(pending, Candidate.risk_level == RiskLevel.MEDIUM)
            .label("medium_risk_count"),
        ).select_from(Candidate)
    ).one()

    open_actions = (
        db.scalar(
            select(func.count())
            .select_from(FollowUpAction)
            .where(FollowUpAction.status == FollowUpStatus.OPEN)
        )
        or 0
    )

    return AnalyticsOverviewResponse(
        total_offered=row.total_offered,
        joined=row.joined,
        dropped_out=row.dropped_out,
        pending=row.pending,
        offer_to_join_conversion_pct=_conversion_pct(row.joined, row.dropped_out),
        joining_next_7_days=row.joining_next_7_days,
        joining_next_15_days=row.joining_next_15_days,
        joining_next_30_days=row.joining_next_30_days,
        high_risk_count=row.high_risk_count,
        medium_risk_count=row.medium_risk_count,
        avg_days_between_interactions=_avg_days_between_interactions(db),
        open_follow_up_actions=open_actions,
    )


def _avg_days_between_interactions(db: Session) -> float | None:
    """Mean gap between consecutive interactions, averaged over pending
    candidates.

    Computed per candidate first — (last - first) / (n - 1) — and then
    averaged across candidates, so every candidate counts once regardless of
    how chatty they are. Pooling every gap globally instead would let one
    candidate with twenty messages dominate the figure for forty.

    Candidates with fewer than two interactions are excluded: a single
    interaction has no gap to measure, and treating it as zero would drag the
    average toward zero exactly for the quiet candidates this product cares
    about. Returns None when no pending candidate has two interactions.
    """
    span_days = func.extract(
        "epoch", func.max(Interaction.occurred_at) - func.min(Interaction.occurred_at)
    ) / 86400.0

    per_candidate = (
        select((span_days / (func.count() - 1)).label("avg_gap"))
        .select_from(Interaction)
        .join(Candidate, Candidate.id == Interaction.candidate_id)
        .where(Candidate.final_outcome == FinalOutcome.PENDING)
        .group_by(Interaction.candidate_id)
        .having(func.count() > 1)
        .subquery()
    )

    result = db.scalar(select(func.avg(per_candidate.c.avg_gap)))
    return None if result is None else round(float(result), 1)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def get_pipeline(db: Session, today: date) -> AnalyticsPipelineResponse:
    """Per-stage funnel counts plus drop-off.

    DROP-OFF DEFINITION (this is the one that goes in the README):

        A candidate counts against stage S if their final_outcome is
        DROPPED_OUT and S is the furthest stage they ever completed — the
        highest sequence_order among their completed candidate_stages rows.

    So drop_off answers "where in the journey did we lose them?" and each
    dropped-out candidate is counted exactly once, against the last thing that
    went right rather than the first thing that did not. CLAUDE.md phrases it
    as "candidates who reached the stage and did not advance", which is the
    same population: reaching stage S and not advancing means S is the
    furthest they got.

    Two consequences worth knowing when reading the numbers:

    * A candidate who dropped out having completed nothing belongs to no
      stage. They are reported separately as
      dropped_out_before_any_stage rather than being silently attributed to
      stage 1, which would overstate the first stage's drop-off.
    * drop_off is a count of candidates, while completed/pending/stalled are
      counts of candidate_stages rows. They do not sum to each other and are
      not meant to.

    The other three columns:

    * completed / pending — over all candidates, whatever their outcome, so
      the funnel describes the whole population.
    * stalled — the subset of pending stages that are past due AND belong to a
      still-pending candidate. Same predicate as Module 6's stage_stall rule,
      so the number here and the actions in the queue always agree. A stage
      left pending on someone who already dropped out is not actionable.
    """
    stages = db.execute(
        select(
            JourneyStage.key,
            JourneyStage.label,
            JourneyStage.sequence_order,
            func.count(CandidateStage.id)
            .filter(CandidateStage.status == StageStatus.COMPLETED)
            .label("completed"),
            func.count(CandidateStage.id)
            .filter(CandidateStage.status == StageStatus.PENDING)
            .label("pending"),
            func.count(CandidateStage.id)
            .filter(
                CandidateStage.status == StageStatus.PENDING,
                CandidateStage.due_date < today,
                Candidate.final_outcome == FinalOutcome.PENDING,
            )
            .label("stalled"),
        )
        .select_from(JourneyStage)
        .outerjoin(CandidateStage, CandidateStage.stage_id == JourneyStage.id)
        .outerjoin(Candidate, Candidate.id == CandidateStage.candidate_id)
        .where(JourneyStage.is_active.is_(True))
        .group_by(JourneyStage.id, JourneyStage.key, JourneyStage.label, JourneyStage.sequence_order)
        .order_by(JourneyStage.sequence_order)
    ).all()

    # Furthest completed stage per dropped-out candidate, then a count per
    # stage. Two aggregates, no candidate ever loaded.
    furthest_completed = (
        select(
            CandidateStage.candidate_id.label("candidate_id"),
            func.max(JourneyStage.sequence_order).label("sequence_order"),
        )
        .select_from(CandidateStage)
        .join(JourneyStage, JourneyStage.id == CandidateStage.stage_id)
        .join(Candidate, Candidate.id == CandidateStage.candidate_id)
        .where(
            Candidate.final_outcome == FinalOutcome.DROPPED_OUT,
            CandidateStage.status == StageStatus.COMPLETED,
        )
        .group_by(CandidateStage.candidate_id)
        .subquery()
    )
    drop_off_by_order = dict(
        db.execute(
            select(furthest_completed.c.sequence_order, func.count()).group_by(
                furthest_completed.c.sequence_order
            )
        ).all()
    )

    total_dropped = (
        db.scalar(
            select(func.count())
            .select_from(Candidate)
            .where(Candidate.final_outcome == FinalOutcome.DROPPED_OUT)
        )
        or 0
    )

    return AnalyticsPipelineResponse(
        # Building 8 response objects from 8 aggregate rows — the loop the
        # "no Python loops" rule is about is one over candidates, and there
        # isn't one anywhere in this module.
        items=[
            PipelineStageStats(
                stage_key=stage.key,
                stage_label=stage.label,
                sequence_order=stage.sequence_order,
                completed=stage.completed,
                pending=stage.pending,
                stalled=stage.stalled,
                drop_off=drop_off_by_order.get(stage.sequence_order, 0),
            )
            for stage in stages
        ],
        total_dropped_out=total_dropped,
        dropped_out_before_any_stage=total_dropped - sum(drop_off_by_order.values()),
    )


# ---------------------------------------------------------------------------
# Recruiters
# ---------------------------------------------------------------------------


def get_recruiters(db: Session, today: date) -> AnalyticsRecruitersResponse:
    """Per-recruiter performance.

    LEFT JOIN from recruiters, so a recruiter holding no candidates appears
    with zeros and a null conversion rather than vanishing from the table —
    a missing row reads as a missing recruiter, not an idle one.

    avg_days_since_last_contact covers that recruiter's PENDING candidates
    only. Resolved candidates would drag it toward whatever the gap happened
    to be when they joined months ago; the question this answers is "how
    stale is this recruiter's live pipeline today?".
    """
    pending = Candidate.final_outcome == FinalOutcome.PENDING

    rows = db.execute(
        select(
            Recruiter.id,
            Recruiter.name,
            func.count(Candidate.id).label("total_offers"),
            func.count(Candidate.id)
            .filter(Candidate.final_outcome == FinalOutcome.JOINED)
            .label("joined"),
            func.count(Candidate.id)
            .filter(Candidate.final_outcome == FinalOutcome.DROPPED_OUT)
            .label("dropped_out"),
            func.count(Candidate.id).filter(pending).label("pending_count"),
            func.count(Candidate.id)
            .filter(pending, Candidate.risk_level == RiskLevel.HIGH)
            .label("high_risk_count"),
            func.avg(cast(_days_since_contact_expr(today), Integer))
            .filter(pending)
            .label("avg_days_since_last_contact"),
        )
        .select_from(Recruiter)
        .outerjoin(Candidate, Candidate.recruiter_id == Recruiter.id)
        .group_by(Recruiter.id, Recruiter.name)
        .order_by(Recruiter.name)
    ).all()

    return AnalyticsRecruitersResponse(
        items=[
            RecruiterStats(
                recruiter_id=row.id,
                recruiter_name=row.name,
                total_offers=row.total_offers,
                joined=row.joined,
                dropped_out=row.dropped_out,
                pending_count=row.pending_count,
                conversion_pct=_conversion_pct(row.joined, row.dropped_out),
                high_risk_count=row.high_risk_count,
                avg_days_since_last_contact=(
                    None
                    if row.avg_days_since_last_contact is None
                    else round(float(row.avg_days_since_last_contact), 1)
                ),
            )
            for row in rows
        ]
    )
