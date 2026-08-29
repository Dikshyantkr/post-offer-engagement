"""Shared constants and plain helper functions for the API tests.

Fixtures live in conftest.py; anything importable (constants, payload
builders, the teardown purge) lives here so test modules can import it
directly rather than reaching into conftest.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import delete, func, select

from app.db import SessionLocal
from app.models import (
    AIAnalysis,
    AuditLog,
    Candidate,
    CandidateStage,
    FollowUpAction,
    Interaction,
)

API = "/api/v1"

# Tables whose row counts must be identical before and after the whole run.
GUARDED_MODELS = (Candidate, CandidateStage, Interaction, AuditLog, FollowUpAction, AIAnalysis)


def row_counts() -> dict[str, int]:
    with SessionLocal() as db:
        return {
            model.__tablename__: db.scalar(select(func.count()).select_from(model)) or 0
            for model in GUARDED_MODELS
        }


def purge_candidates(candidate_ids: list[str]) -> None:
    """Delete test-created candidates and everything hanging off them.

    audit_log has no foreign key, so its rows are matched on entity_id for
    both the candidates themselves and their candidate_stages rows.
    """
    if not candidate_ids:
        return

    ids = [uuid.UUID(cid) for cid in candidate_ids]
    with SessionLocal() as db:
        stage_ids = list(
            db.scalars(select(CandidateStage.id).where(CandidateStage.candidate_id.in_(ids)))
        )
        db.execute(delete(AuditLog).where(AuditLog.entity_id.in_(ids + stage_ids)))
        db.execute(delete(Interaction).where(Interaction.candidate_id.in_(ids)))
        db.execute(delete(CandidateStage).where(CandidateStage.candidate_id.in_(ids)))
        db.execute(delete(AIAnalysis).where(AIAnalysis.candidate_id.in_(ids)))
        db.execute(delete(FollowUpAction).where(FollowUpAction.candidate_id.in_(ids)))
        db.execute(delete(Candidate).where(Candidate.id.in_(ids)))
        db.commit()


def unique_email(prefix: str = "test") -> str:
    return f"{prefix}.{uuid.uuid4().hex[:10]}@example.com"


def candidate_payload(recruiter_id: str, **overrides) -> dict:
    offer_date = date.today() - timedelta(days=10)
    payload = {
        "name": "Test Candidate",
        "email": unique_email(),
        "role": "Software Engineer II",
        "department": "Engineering",
        "location": "Bengaluru",
        "offer_date": offer_date.isoformat(),
        "joining_date": (offer_date + timedelta(days=60)).isoformat(),
        "recruiter_id": recruiter_id,
    }
    payload.update(overrides)
    return payload
