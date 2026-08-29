from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import RecruiterListResponse, RecruiterResponse
from app.services import recruiter_service

router = APIRouter(prefix="/recruiters", tags=["recruiters"])


@router.get("", response_model=RecruiterListResponse)
def list_recruiters(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> RecruiterListResponse:
    items, total = recruiter_service.list_recruiters(db, limit=limit, offset=offset)
    return RecruiterListResponse(
        items=[RecruiterResponse.model_validate(r) for r in items],
        total=total,
        limit=limit,
        offset=offset,
    )
