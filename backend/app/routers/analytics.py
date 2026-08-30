"""Module 8 read-only analytics. No logic here — everything is in SQL, in
analytics_service.

`today` is resolved per request rather than at import so a long-running
container does not keep reporting the date it booted on.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import (
    AnalyticsOverviewResponse,
    AnalyticsPipelineResponse,
    AnalyticsRecruitersResponse,
)
from app.services import analytics_service

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/overview", response_model=AnalyticsOverviewResponse)
def overview(db: Session = Depends(get_db)) -> AnalyticsOverviewResponse:
    """Headline funnel and workload numbers for the dashboard."""
    return analytics_service.get_overview(db, date.today())


@router.get("/pipeline", response_model=AnalyticsPipelineResponse)
def pipeline(db: Session = Depends(get_db)) -> AnalyticsPipelineResponse:
    """Per-stage funnel counts and drop-off, in journey sequence order.

    Drop-off attributes each dropped-out candidate to the furthest stage they
    completed — see analytics_service.get_pipeline for the full definition.
    """
    return analytics_service.get_pipeline(db, date.today())


@router.get("/recruiters", response_model=AnalyticsRecruitersResponse)
def recruiters(db: Session = Depends(get_db)) -> AnalyticsRecruitersResponse:
    """Per-recruiter offers, outcomes, conversion and pipeline staleness."""
    return analytics_service.get_recruiters(db, date.today())
