from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.routers import get_actor
from app.schemas import CandidateStageResponse
from app.services import stage_service

router = APIRouter(prefix="/candidates/{candidate_id}/stages", tags=["stages"])


@router.get("", response_model=list[CandidateStageResponse])
def list_stages(candidate_id: uuid.UUID, db: Session = Depends(get_db)) -> list[CandidateStageResponse]:
    stages = stage_service.list_candidate_stages(db, candidate_id)
    return [stage_service.to_response(cs) for cs in stages]


@router.post("/{stage_id}/complete", response_model=CandidateStageResponse)
def complete_stage(
    candidate_id: uuid.UUID,
    stage_id: uuid.UUID,
    actor: str = Depends(get_actor),
    db: Session = Depends(get_db),
) -> CandidateStageResponse:
    candidate_stage = stage_service.complete_stage(db, candidate_id, stage_id, actor)
    return stage_service.to_response(candidate_stage)
