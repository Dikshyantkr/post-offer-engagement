"""candidate_stages queries and the stage-completion transition.

Stage due dates are computed once, at candidate creation (candidate_service),
by the pure compute_stage_schedule function — this module never recomputes
them, it only reads and transitions status.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.enums import StageStatus
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

    db.commit()
    db.refresh(candidate_stage)
    return candidate_stage
