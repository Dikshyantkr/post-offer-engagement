"""Interaction queries and creation. Writing an interaction updates
candidates.last_interaction_at in the same transaction — no separate commit
in between, so the two are always consistent.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import NotFoundError
from app.models import Candidate, Interaction
from app.schemas import InteractionCreate
from app.services import risk_service


def list_interactions(
    db: Session, candidate_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[Interaction], int]:
    if db.get(Candidate, candidate_id) is None:
        raise NotFoundError(f"Candidate {candidate_id} not found")

    base = select(Interaction).where(Interaction.candidate_id == candidate_id)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    stmt = base.order_by(Interaction.occurred_at.desc()).limit(limit).offset(offset)
    items = list(db.scalars(stmt).all())
    return items, total


def create_interaction(
    db: Session, candidate_id: uuid.UUID, payload: InteractionCreate, actor: str
) -> Interaction:
    candidate = db.get(Candidate, candidate_id)
    if candidate is None:
        raise NotFoundError(f"Candidate {candidate_id} not found")

    occurred_at = payload.occurred_at or datetime.now(timezone.utc)

    interaction = Interaction(
        candidate=candidate,
        channel=payload.channel,
        direction=payload.direction,
        content=payload.content,
        occurred_at=occurred_at,
        created_by=actor,
        blocker_raised=payload.blocker_raised,
        blocker_category=payload.blocker_category,
        date_confirmed=payload.date_confirmed,
        recruiter_read=payload.recruiter_read,
    )
    db.add(interaction)

    if candidate.last_interaction_at is None or occurred_at > candidate.last_interaction_at:
        candidate.last_interaction_at = occurred_at

    # The session runs with autoflush=False, so the new interaction must be
    # flushed before the recompute's SELECT can see it — otherwise a call note
    # logging a counter-offer would not raise risk until the next sweep.
    db.flush()

    # Same transaction as the interaction write: logging contact immediately
    # reflects in the candidate's risk, and the two can never disagree.
    risk_service.recompute_for_candidate(db, candidate, date.today(), actor)

    db.commit()
    db.refresh(interaction)
    return interaction
