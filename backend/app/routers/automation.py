"""Manual trigger for the engagement sweep — the demo entry point.

Calls the same `run_engagement_sweep` the scheduler calls, so the automation
rules a reviewer exercises here are the ones that run at 02:00 UTC.

One deliberate difference: the scheduled job rescores every pending candidate
(risk_service.recompute_all) before sweeping, and this endpoint does not. Risk
scoring is already exposed on its own at POST /risk/recompute, and folding it
in here would make a "run the automation" button quietly rewrite every risk
badge in the database — a surprising amount of state change for one click. Run
the two in that order to reproduce a full nightly pass.
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
