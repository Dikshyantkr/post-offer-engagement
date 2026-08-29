"""Not in CLAUDE.md's explicit service-file list, but recruiters.py is one of
the five listed routers and "no business logic in routers" is an
unconditional rule — so its one query lives here rather than inline.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Recruiter


def list_recruiters(db: Session, *, limit: int, offset: int) -> tuple[list[Recruiter], int]:
    total = db.scalar(select(func.count()).select_from(Recruiter)) or 0
    stmt = select(Recruiter).order_by(Recruiter.name).limit(limit).offset(offset)
    items = list(db.scalars(stmt).all())
    return items, total
