"""Candidate CRUD, filtering, and the candidate-detail assembly.

POST /candidates materialises the 8 candidate_stages rows via the existing,
pure compute_stage_schedule function — this module never reimplements that
due-date logic, only calls it. PATCH reuses the same function to reschedule
stages if offer_date/joining_date changes, and flips risk_source to
'hr_override' whenever risk_level is part of the patch.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.enums import CandidateSort, EngagementStatus, FollowUpStatus, RiskLevel, RiskSource
from app.errors import ConfigurationError, NotFoundError
from app.models import Candidate, CandidateStage, JourneyStage, Recruiter
from app.schemas import (
    CandidateCreate,
    CandidateDetailResponse,
    CandidateResponse,
    CandidateStageResponse,
    CandidateUpdate,
)
from app.services import audit_service, risk_service, stage_service
from app.services.stage_scheduler import compute_stage_schedule


def _risk_ordering() -> list:
    """Order by risk band first, then severity within the band.

    Two keys, not one, because risk_score_base alone is the wrong sort. The
    score is the *rule floor*, and an AI assessment can raise a candidate's
    band above it — Sana Qureshi sits at a rule floor of 50.1 (MEDIUM) while
    her final level is HIGH, because the counter-offer signal is in a WhatsApp
    message the rules cannot read. Sorting on the score alone would file her
    below every rule-flagged candidate, which is exactly backwards: she is the
    one worth calling first.

    So: final band descending (what the recruiter sees on the badge), then
    risk_score_base descending to rank within the band (what the score exists
    for), then joining_date to break ties toward whoever joins soonest.

    The band ranking comes from risk_service.BAND_RANK rather than the
    database enum's declaration order, so "which band outranks which" has one
    definition.

    Built from explicit `==` comparisons rather than case(BAND_RANK,
    value=...): RiskLevel subclasses str, so the shorthand renders its keys as
    the enum *values* ("low"), while SQLAlchemy stores enum *names* ("LOW").
    Comparing against the column routes each literal through the column's own
    Enum type and binds the name.
    """
    band = case(
        *[(Candidate.risk_level == level, rank) for level, rank in risk_service.BAND_RANK.items()],
        else_=0,
    )
    return [band.desc(), Candidate.risk_score_base.desc(), Candidate.joining_date.asc()]


def list_candidates(
    db: Session,
    *,
    limit: int,
    offset: int,
    joining_month: str | None = None,
    recruiter_id: uuid.UUID | None = None,
    role: str | None = None,
    risk_level: RiskLevel | None = None,
    engagement_status: EngagementStatus | None = None,
    search: str | None = None,
    joining_within_days: int | None = None,
    sort: CandidateSort = CandidateSort.JOINING_DATE,
) -> tuple[list[Candidate], int]:
    stmt = select(Candidate)

    if joining_month is not None:
        year_str, month_str = joining_month.split("-")
        year, month = int(year_str), int(month_str)
        start = date(year, month, 1)
        end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        stmt = stmt.where(Candidate.joining_date >= start, Candidate.joining_date < end)

    if recruiter_id is not None:
        stmt = stmt.where(Candidate.recruiter_id == recruiter_id)

    if role is not None:
        stmt = stmt.where(Candidate.role == role)

    if risk_level is not None:
        stmt = stmt.where(Candidate.risk_level == risk_level)

    if engagement_status is not None:
        stmt = stmt.where(Candidate.engagement_status == engagement_status)

    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Candidate.name.ilike(pattern),
                Candidate.email.ilike(pattern),
                Candidate.role.ilike(pattern),
            )
        )

    if joining_within_days is not None:
        stmt = stmt.where(risk_service.joining_within(date.today(), joining_within_days))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    ordering = (
        _risk_ordering()
        if sort == CandidateSort.RISK
        # id as a final tiebreaker: without it, candidates sharing a joining
        # date can come back in any order, and two pages of the same list can
        # then show the same candidate twice or skip one.
        else [Candidate.joining_date.asc(), Candidate.id.asc()]
    )
    stmt = stmt.order_by(*ordering).limit(limit).offset(offset)
    items = list(db.scalars(stmt).all())
    return items, total


def create_candidate(db: Session, payload: CandidateCreate) -> Candidate:
    recruiter = db.get(Recruiter, payload.recruiter_id)
    if recruiter is None:
        raise NotFoundError(f"Recruiter {payload.recruiter_id} not found")

    candidate = Candidate(
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        role=payload.role,
        department=payload.department,
        location=payload.location,
        offer_date=payload.offer_date,
        joining_date=payload.joining_date,
        recruiter=recruiter,
        notes=payload.notes,
    )
    db.add(candidate)

    stages = list(
        db.scalars(
            select(JourneyStage)
            .where(JourneyStage.is_active.is_(True))
            .order_by(JourneyStage.sequence_order)
        )
    )
    if not stages:
        raise ConfigurationError(
            "No active journey_stages are configured, so the candidate's stage "
            "timeline cannot be materialised. Seed the journey_stages template first."
        )

    schedule = compute_stage_schedule(candidate.offer_date, candidate.joining_date, stages)
    stage_by_key = {s.key: s for s in stages}
    for key, due_date in schedule:
        candidate.stages.append(CandidateStage(stage=stage_by_key[key], due_date=due_date))

    # No audit_log row here: CLAUDE.md scopes audit writes to candidate
    # *update*, stage transition, and override — not initial creation.
    db.commit()
    db.refresh(candidate)
    return candidate


def _load_candidate_for_detail(db: Session, candidate_id: uuid.UUID) -> Candidate:
    stmt = (
        select(Candidate)
        .where(Candidate.id == candidate_id)
        .options(
            selectinload(Candidate.stages).selectinload(CandidateStage.stage),
            selectinload(Candidate.interactions),
            selectinload(Candidate.ai_analyses),
            selectinload(Candidate.follow_up_actions),
        )
    )
    candidate = db.scalar(stmt)
    if candidate is None:
        raise NotFoundError(f"Candidate {candidate_id} not found")
    return candidate


def get_candidate_detail(db: Session, candidate_id: uuid.UUID) -> CandidateDetailResponse:
    candidate = _load_candidate_for_detail(db, candidate_id)

    stages = sorted(candidate.stages, key=lambda cs: cs.stage.sequence_order)
    interactions = sorted(candidate.interactions, key=lambda i: i.occurred_at, reverse=True)
    latest_ai_analysis = max(candidate.ai_analyses, key=lambda a: a.created_at, default=None)
    open_actions = sorted(
        (a for a in candidate.follow_up_actions if a.status == FollowUpStatus.OPEN),
        key=lambda a: (a.due_date is None, a.due_date),
    )

    return CandidateDetailResponse(
        **CandidateResponse.model_validate(candidate).model_dump(),
        stages=[stage_service.to_response(cs) for cs in stages],
        interactions=list(interactions),
        latest_ai_analysis=latest_ai_analysis,
        open_actions=list(open_actions),
    )


def _reschedule_stages(db: Session, candidate: Candidate) -> None:
    # Same is_active filter as create_candidate: an inactive stage left in the
    # set would widen the compression window and produce different due dates
    # here than the candidate was originally created with.
    all_stages = list(db.scalars(select(JourneyStage).where(JourneyStage.is_active.is_(True))))
    schedule = dict(compute_stage_schedule(candidate.offer_date, candidate.joining_date, all_stages))
    stage_by_id = {s.id: s for s in all_stages}

    candidate_stages = db.scalars(
        select(CandidateStage).where(CandidateStage.candidate_id == candidate.id)
    ).all()
    for cs in candidate_stages:
        stage = stage_by_id.get(cs.stage_id)
        if stage is not None and stage.key in schedule:
            cs.due_date = schedule[stage.key]


def update_candidate(
    db: Session, candidate_id: uuid.UUID, payload: CandidateUpdate, actor: str
) -> Candidate:
    candidate = db.get(Candidate, candidate_id)
    if candidate is None:
        raise NotFoundError(f"Candidate {candidate_id} not found")

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return candidate

    if "recruiter_id" in changes:
        if db.get(Recruiter, changes["recruiter_id"]) is None:
            raise NotFoundError(f"Recruiter {changes['recruiter_id']} not found")

    before = CandidateResponse.model_validate(candidate).model_dump(mode="json")

    reschedule = "offer_date" in changes or "joining_date" in changes

    for field, value in changes.items():
        setattr(candidate, field, value)

    if "risk_level" in changes:
        candidate.risk_source = RiskSource.HR_OVERRIDE

    if reschedule:
        _reschedule_stages(db, candidate)

    db.flush()
    after = CandidateResponse.model_validate(candidate).model_dump(mode="json")

    if before != after:
        audit_service.record(
            db,
            entity_type="candidate",
            entity_id=candidate.id,
            action="update",
            actor=actor,
            before=before,
            after=after,
        )

    db.commit()
    db.refresh(candidate)
    return candidate
