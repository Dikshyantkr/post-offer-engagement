"""The five Module 5 endpoints. No logic here — everything calls ai_service.

All four analysis endpoints return 200 even when the provider is unreachable.
That is a deliberate departure from the 503-on-LLM-failure line in CLAUDE.md's
Module 2 error table, and CLAUDE.md's own Module 5 rule is the one being
followed: "return a deterministic rule-based fallback, set was_fallback=True,
return 200 with the flag. The app must never break because the LLM misbehaved."
A recruiter's morning triage does not stop because a model was rate-limited.
The caller is told exactly what happened through `meta.was_fallback` and
`meta.validation_status`; it is signalled, not hidden. LLMProviderError and its
503 remain wired up in errors.py for any future path that genuinely cannot
degrade.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.ai.engine import AIResult
from app.ai.provider import LLMProvider, get_provider
from app.db import get_db
from app.routers import get_actor
from app.schemas import (
    AIAnalysisMeta,
    AIOverrideRequest,
    AIOverrideResponse,
    AssessRiskResponse,
    CandidateResponse,
    DraftMessageRequest,
    DraftMessageResponse,
    RecommendActionResponse,
    RiskApplicationResponse,
    SummarizeResponse,
)
from app.services import ai_service

router = APIRouter(prefix="/ai", tags=["ai"])


def get_llm_provider() -> LLMProvider:
    """Injected rather than imported so tests can substitute a fake through
    FastAPI's dependency_overrides and never touch the real API."""
    return get_provider()


def _meta(result: AIResult) -> AIAnalysisMeta:
    return AIAnalysisMeta(
        analysis_id=result.analysis_id,
        analysis_type=result.analysis_type,
        model_name=result.model_name,
        prompt_version=result.prompt_version,
        validation_status=result.validation_status,
        was_fallback=result.was_fallback,
        latency_ms=result.latency_ms,
        confidence=result.confidence,
        created_at=result.created_at,
    )


@router.post("/candidates/{candidate_id}/assess-risk", response_model=AssessRiskResponse)
def assess_risk(
    candidate_id: uuid.UUID,
    actor: str = Depends(get_actor),
    db: Session = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
) -> AssessRiskResponse:
    """Read the candidate's own messages and re-score their risk.

    The AI may raise the level above the rule floor; it can never lower it,
    and an HR override beats both.
    """
    _candidate, result, application = ai_service.assess_risk(
        db, candidate_id, actor, provider=provider
    )
    return AssessRiskResponse(
        meta=_meta(result),
        assessment=result.output,
        risk=RiskApplicationResponse(**vars(application)),
    )


@router.post("/candidates/{candidate_id}/summarize", response_model=SummarizeResponse)
def summarize(
    candidate_id: uuid.UUID,
    db: Session = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
) -> SummarizeResponse:
    result = ai_service.summarize(db, candidate_id, provider=provider)
    return SummarizeResponse(meta=_meta(result), summary=result.output)


@router.post("/candidates/{candidate_id}/recommend-action", response_model=RecommendActionResponse)
def recommend_action(
    candidate_id: uuid.UUID,
    db: Session = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
) -> RecommendActionResponse:
    result = ai_service.recommend_action(db, candidate_id, provider=provider)
    return RecommendActionResponse(meta=_meta(result), recommendation=result.output)


@router.post("/candidates/{candidate_id}/draft-message", response_model=DraftMessageResponse)
def draft_message(
    candidate_id: uuid.UUID,
    payload: DraftMessageRequest,
    db: Session = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
) -> DraftMessageResponse:
    """Draft a message for the recruiter to edit and send. Nothing is sent."""
    result = ai_service.draft_message(
        db,
        candidate_id,
        channel=payload.channel,
        intent=payload.intent,
        tone=payload.tone,
        provider=provider,
    )
    return DraftMessageResponse(
        meta=_meta(result),
        draft=result.output,
        guardrails_removed=result.guardrails_removed,
    )


@router.post("/candidates/{candidate_id}/override", response_model=AIOverrideResponse)
def override(
    candidate_id: uuid.UUID,
    payload: AIOverrideRequest,
    actor: str = Depends(get_actor),
    db: Session = Depends(get_db),
) -> AIOverrideResponse:
    """Record that a human disagreed with the AI, on the risk level, the
    recommended action, or both. No provider is involved."""
    candidate, recorded = ai_service.override(db, candidate_id, payload, actor)
    return AIOverrideResponse(
        candidate=CandidateResponse.model_validate(candidate), recorded=recorded
    )
