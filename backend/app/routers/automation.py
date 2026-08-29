"""Manual trigger for the nightly sweep — the demo entry point.

The same `run_engagement_sweep` the scheduler calls, so what a reviewer sees
here is exactly what runs at 02:00 UTC, not a separate demo path that could
drift from it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.ai.provider import LLMProvider
from app.db import get_db
from app.routers import get_actor
from app.routers.ai import get_llm_provider
from app.schemas import AutomationRunResponse
from app.services import automation_service

router = APIRouter(prefix="/automation", tags=["automation"])


@router.post("/run", response_model=AutomationRunResponse)
def run_sweep(
    actor: str = Depends(get_actor),
    db: Session = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
) -> AutomationRunResponse:
    """Run both automation rules now and report what happened.

    Safe to call repeatedly: the idempotency window means a second run inside
    24 hours creates nothing and reports the skips instead.
    """
    summary = automation_service.run_engagement_sweep(db, actor=actor, provider=provider)
    return AutomationRunResponse(**summary.as_dict())
