from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.enums import CandidateSort, EngagementStatus, RiskLevel
from app.routers import get_actor
from app.schemas import (
    CandidateCreate,
    CandidateDetailResponse,
    CandidateListResponse,
    CandidateResponse,
    CandidateUpdate,
)
from app.services import candidate_service

router = APIRouter(prefix="/candidates", tags=["candidates"])


@router.get("", response_model=CandidateListResponse)
def list_candidates(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    joining_month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    recruiter_id: uuid.UUID | None = None,
    role: str | None = None,
    risk_level: RiskLevel | None = None,
    engagement_status: EngagementStatus | None = None,
    search: str | None = None,
    joining_within_days: int | None = Query(None, ge=0),
    sort: CandidateSort = Query(
        CandidateSort.JOINING_DATE,
        description=(
            "joining_date (default) or risk. 'risk' orders by final risk band, "
            "then by risk_score_base within the band — the triage view."
        ),
    ),
    db: Session = Depends(get_db),
) -> CandidateListResponse:
    items, total = candidate_service.list_candidates(
        db,
        limit=limit,
        offset=offset,
        joining_month=joining_month,
        recruiter_id=recruiter_id,
        role=role,
        risk_level=risk_level,
        engagement_status=engagement_status,
        search=search,
        joining_within_days=joining_within_days,
        sort=sort,
    )
    return CandidateListResponse(
        items=[CandidateResponse.model_validate(c) for c in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=CandidateResponse, status_code=201)
def create_candidate(payload: CandidateCreate, db: Session = Depends(get_db)) -> CandidateResponse:
    candidate = candidate_service.create_candidate(db, payload)
    return CandidateResponse.model_validate(candidate)


@router.get("/{candidate_id}", response_model=CandidateDetailResponse)
def get_candidate(candidate_id: uuid.UUID, db: Session = Depends(get_db)) -> CandidateDetailResponse:
    return candidate_service.get_candidate_detail(db, candidate_id)


@router.patch("/{candidate_id}", response_model=CandidateResponse)
def update_candidate(
    candidate_id: uuid.UUID,
    payload: CandidateUpdate,
    actor: str = Depends(get_actor),
    db: Session = Depends(get_db),
) -> CandidateResponse:
    candidate = candidate_service.update_candidate(db, candidate_id, payload, actor)
    return CandidateResponse.model_validate(candidate)
