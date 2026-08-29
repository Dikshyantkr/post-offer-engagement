"""candidate_stages queries and the stage-completion transition.

Stage due dates are computed once, at candidate creation (candidate_service),
by the pure compute_stage_schedule function — this module never recomputes
them, it only reads and transitions status.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.enums import (
    STAGE_KEY_TO_ENGAGEMENT_STATUS,
    EngagementStatus,
    FinalOutcome,
    StageStatus,
)
from app.errors import BadStateTransitionError, NotFoundError
from app.models import Candidate, CandidateStage
from app.schemas import CandidateStageResponse
from app.services import audit_service


def to_response(candidate_stage: CandidateStage) -> CandidateStageResponse:
    stage = candidate_stage.stage
    return CandidateStageResponse(
        id=candidate_stage.id,
        candidate_id=candidate_stage.candidate_id,
        stage_id=candidate_stage.stage_id,
        stage_key=stage.key,
        stage_label=stage.label,
        sequence_order=stage.sequence_order,
        anchor=stage.anchor,
        due_date=candidate_stage.due_date,
        status=candidate_stage.status,
        completed_at=candidate_stage.completed_at,
        completed_by=candidate_stage.completed_by,
    )


def resolve_engagement_status(candidate_stages: Iterable[CandidateStage]) -> EngagementStatus:
    """Derive engagement_status from the furthest-along completed stage.

    Shared by seed.py and complete_stage so both produce identical results.
    Each element must have its `stage` relationship loaded.
    """
    completed = [cs for cs in candidate_stages if cs.status == StageStatus.COMPLETED]
    completed.sort(key=lambda cs: cs.stage.sequence_order, reverse=True)
    for candidate_stage in completed:
        mapped = STAGE_KEY_TO_ENGAGEMENT_STATUS.get(candidate_stage.stage.key)
        if mapped is not None:
            return mapped
    return EngagementStatus.OFFER_ACCEPTED


def list_candidate_stages(db: Session, candidate_id: uuid.UUID) -> list[CandidateStage]:
    if db.get(Candidate, candidate_id) is None:
        raise NotFoundError(f"Candidate {candidate_id} not found")

    stmt = (
        select(CandidateStage)
        .where(CandidateStage.candidate_id == candidate_id)
        .options(selectinload(CandidateStage.stage))
    )
    stages = list(db.scalars(stmt).all())
    stages.sort(key=lambda cs: cs.stage.sequence_order)
    return stages


def complete_stage(
    db: Session, candidate_id: uuid.UUID, stage_id: uuid.UUID, actor: str
) -> CandidateStage:
    candidate_stage = db.scalar(
        select(CandidateStage)
        .where(
            CandidateStage.candidate_id == candidate_id,
            CandidateStage.stage_id == stage_id,
        )
        .options(selectinload(CandidateStage.stage))
    )
    if candidate_stage is None:
        raise NotFoundError(f"Stage {stage_id} not found for candidate {candidate_id}")

    if candidate_stage.status in (StageStatus.COMPLETED, StageStatus.SKIPPED):
        raise BadStateTransitionError(
            f"Stage '{candidate_stage.stage.key}' is already {candidate_stage.status.value} "
            "and cannot be completed again"
        )

    before = {
        "status": candidate_stage.status.value,
        "completed_at": None,
        "completed_by": candidate_stage.completed_by,
    }

    candidate_stage.status = StageStatus.COMPLETED
    candidate_stage.completed_at = datetime.now(timezone.utc)
    candidate_stage.completed_by = actor

    after = {
        "status": candidate_stage.status.value,
        "completed_at": candidate_stage.completed_at.isoformat(),
        "completed_by": candidate_stage.completed_by,
    }

    audit_service.record(
        db,
        entity_type="candidate_stage",
        entity_id=candidate_stage.id,
        action="stage_complete",
        actor=actor,
        before=before,
        after=after,
    )

    _advance_engagement_status(db, candidate_id, actor)

    db.commit()
    db.refresh(candidate_stage)
    return candidate_stage


def _advance_engagement_status(db: Session, candidate_id: uuid.UUID, actor: str) -> None:
    """Recompute the candidate's engagement_status from its completed stages.

    Part of the same transaction as the stage transition — the two commit
    together or not at all. A candidate who has already joined or dropped out
    keeps that terminal status; ticking off a stage must not walk it back.
    """
    candidate = db.get(Candidate, candidate_id)
    if candidate is None or candidate.final_outcome != FinalOutcome.PENDING:
        return

    stages = db.scalars(
        select(CandidateStage)
        .where(CandidateStage.candidate_id == candidate_id)
        .options(selectinload(CandidateStage.stage))
    ).all()

    resolved = resolve_engagement_status(stages)
    if resolved == candidate.engagement_status:
        return

    previous = candidate.engagement_status
    candidate.engagement_status = resolved

    audit_service.record(
        db,
        entity_type="candidate",
        entity_id=candidate.id,
        action="engagement_status_advance",
        actor=actor,
        before={"engagement_status": previous.value},
        after={"engagement_status": resolved.value},
    )
