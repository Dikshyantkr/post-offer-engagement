"""Not in CLAUDE.md's explicit service-file list, same reasoning as
recruiter_service.py — follow_up_actions.py is a listed router and must stay
logic-free.

No audit_log writes here: CLAUDE.md scopes audit_log to candidate update,
stage transition, and override — follow-up actions aren't in that list.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import FollowUpStatus
from app.errors import NotFoundError
from app.models import Candidate, FollowUpAction
from app.schemas import FollowUpActionUpdate


def list_follow_up_actions(
    db: Session,
    *,
    limit: int,
    offset: int,
    status: FollowUpStatus | None = None,
    recruiter_id: uuid.UUID | None = None,
) -> tuple[list[FollowUpAction], int]:
    stmt = select(FollowUpAction)

    if recruiter_id is not None:
        stmt = stmt.join(Candidate, Candidate.id == FollowUpAction.candidate_id).where(
            Candidate.recruiter_id == recruiter_id
        )

    if status is not None:
        stmt = stmt.where(FollowUpAction.status == status)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    stmt = stmt.order_by(FollowUpAction.created_at.desc()).limit(limit).offset(offset)
    items = list(db.scalars(stmt).all())
    return items, total


def update_follow_up_action(
    db: Session, action_id: uuid.UUID, payload: FollowUpActionUpdate
) -> FollowUpAction:
    action = db.get(FollowUpAction, action_id)
    if action is None:
        raise NotFoundError(f"Follow-up action {action_id} not found")

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return action

    for field, value in changes.items():
        setattr(action, field, value)

    if "status" in changes:
        if action.status in (FollowUpStatus.DONE, FollowUpStatus.DISMISSED):
            action.completed_at = datetime.now(timezone.utc)
        elif action.status == FollowUpStatus.OPEN:
            action.completed_at = None

    db.commit()
    db.refresh(action)
    return action
