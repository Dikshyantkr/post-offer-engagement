from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.enums import FollowUpStatus
from app.schemas import FollowUpActionListResponse, FollowUpActionResponse, FollowUpActionUpdate
from app.services import follow_up_service

router = APIRouter(prefix="/follow-up-actions", tags=["follow-up-actions"])


@router.get("", response_model=FollowUpActionListResponse)
def list_follow_up_actions(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: FollowUpStatus | None = None,
    recruiter_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
) -> FollowUpActionListResponse:
    items, total = follow_up_service.list_follow_up_actions(
        db, limit=limit, offset=offset, status=status, recruiter_id=recruiter_id
    )
    return FollowUpActionListResponse(
        items=[FollowUpActionResponse.model_validate(a) for a in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch("/{action_id}", response_model=FollowUpActionResponse)
def update_follow_up_action(
    action_id: uuid.UUID,
    payload: FollowUpActionUpdate,
    db: Session = Depends(get_db),
) -> FollowUpActionResponse:
    action = follow_up_service.update_follow_up_action(db, action_id, payload)
    return FollowUpActionResponse.model_validate(action)
