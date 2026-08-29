from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.routers import get_actor
from app.schemas import InteractionCreate, InteractionListResponse, InteractionResponse
from app.services import interaction_service

router = APIRouter(prefix="/candidates/{candidate_id}/interactions", tags=["interactions"])


@router.get("", response_model=InteractionListResponse)
def list_interactions(
    candidate_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> InteractionListResponse:
    items, total = interaction_service.list_interactions(db, candidate_id, limit=limit, offset=offset)
    return InteractionListResponse(
        items=[InteractionResponse.model_validate(i) for i in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=InteractionResponse, status_code=201)
def create_interaction(
    candidate_id: uuid.UUID,
    payload: InteractionCreate,
    actor: str = Depends(get_actor),
    db: Session = Depends(get_db),
) -> InteractionResponse:
    interaction = interaction_service.create_interaction(db, candidate_id, payload, actor)
    return InteractionResponse.model_validate(interaction)
