from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.routers import get_actor
from app.schemas import RiskRecomputeResponse
from app.services import risk_service

router = APIRouter(prefix="/risk", tags=["risk"])


@router.post("/recompute", response_model=RiskRecomputeResponse)
def recompute_risk(
    actor: str = Depends(get_actor),
    db: Session = Depends(get_db),
) -> RiskRecomputeResponse:
    """Rescore every pending candidate against the rule floor.

    Runs nightly in Module 6; exposed here so it is demoable on demand.
    """
    return RiskRecomputeResponse(**risk_service.recompute_all(db, date.today(), actor))
