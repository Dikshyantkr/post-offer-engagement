"""Application-level orchestration for the Module 5 AI endpoints.

ai/engine.py produces an analysis. This module decides what the application
does about it — and for the risk assessment, that decision is the whole point
of Module 4's two-layer design:

    final = max(rule_floor, ai_assessment)     the AI may only RAISE
    hr_override beats both                     a human decision is final

`_apply_risk_assessment` below is the only code in the app that writes
risk_source='ai'. Keeping it in one function is what makes "the AI can never
talk us out of worrying about someone" an auditable property rather than an
aspiration: there is a single place to check.

Routers call this module; this module calls the engine and risk_service.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import engine
from app.ai.contracts import DraftedMessage, InteractionSummary, NextAction, RiskAssessment
from app.ai.engine import AIResult
from app.ai.provider import LLMProvider
from app.enums import FinalOutcome, RiskLevel, RiskSource
from app.errors import NotFoundError
from app.models import AIAnalysis, Candidate
from app.schemas import AIOverrideRequest
from app.services import audit_service, risk_service

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskApplication:
    """How the AI's answer interacted with the rule floor, in full.

    Every field here is surfaced to the UI. CLAUDE.md is explicit that a risk
    badge without visible reasoning reads as magic, and half of that reasoning
    is provenance: a recruiter needs to see that the rules said medium, the AI
    said high after reading the candidate's messages, and high is what won.
    """

    rule_floor_level: RiskLevel
    rule_floor_score: float
    ai_level: RiskLevel
    final_level: RiskLevel
    risk_source: RiskSource
    raised_by_ai: bool
    applied: bool
    note: str


def _get_candidate(db: Session, candidate_id: uuid.UUID) -> Candidate:
    candidate = db.get(Candidate, candidate_id)
    if candidate is None:
        raise NotFoundError(f"Candidate {candidate_id} not found")
    return candidate


# ---------------------------------------------------------------------------
# Risk assessment + application
# ---------------------------------------------------------------------------


def _apply_risk_assessment(
    db: Session,
    candidate: Candidate,
    ai_level: RiskLevel,
    *,
    analysis_id: uuid.UUID,
    was_fallback: bool,
    actor: str,
    today: date,
) -> RiskApplication:
    floor = risk_service.rule_floor_for_candidate(db, candidate, today)

    if candidate.final_outcome != FinalOutcome.PENDING:
        # Joined or dropped out: "will they show up?" is settled, and
        # risk_service excludes them from scoring for the same reason. The
        # analysis is still persisted — a recruiter may legitimately want to
        # run one on a candidate who dropped out — it just changes nothing.
        return RiskApplication(
            rule_floor_level=floor.level,
            rule_floor_score=floor.score,
            ai_level=ai_level,
            final_level=candidate.risk_level,
            risk_source=candidate.risk_source,
            raised_by_ai=False,
            applied=False,
            note=(
                f"Candidate's final outcome is already '{candidate.final_outcome.value}', "
                "so their risk level was left untouched."
            ),
        )

    # Record the floor even when it does not win, so the UI can show it beside
    # the final badge.
    candidate.risk_score_base = floor.score

    if candidate.risk_source == RiskSource.HR_OVERRIDE:
        return RiskApplication(
            rule_floor_level=floor.level,
            rule_floor_score=floor.score,
            ai_level=ai_level,
            final_level=candidate.risk_level,
            risk_source=RiskSource.HR_OVERRIDE,
            raised_by_ai=False,
            applied=False,
            note=(
                "A human has overridden this candidate's risk level, which beats both the "
                "rules and the AI. The assessment was saved but not applied."
            ),
        )

    raised = risk_service.is_higher(ai_level, floor.level)
    final_level = ai_level if raised else floor.level
    final_source = RiskSource.AI if raised else RiskSource.RULE

    note = (
        f"AI read the candidate's messages and raised risk from the {floor.level.value} rule "
        f"floor to {ai_level.value}."
        if raised
        else (
            f"AI assessed {ai_level.value}, at or below the {floor.level.value} rule floor. "
            "The floor stands — an AI assessment can only raise risk, never lower it."
        )
    )
    if was_fallback:
        note += " (Provider unavailable: this assessment is the deterministic fallback.)"

    if (candidate.risk_level, candidate.risk_source) != (final_level, final_source):
        audit_service.record(
            db,
            entity_type="candidate",
            entity_id=candidate.id,
            action="ai_risk_assessment",
            actor=actor,
            before={
                "risk_level": candidate.risk_level.value,
                "risk_source": candidate.risk_source.value,
            },
            after={
                "risk_level": final_level.value,
                "risk_source": final_source.value,
                "rule_floor_level": floor.level.value,
                "ai_level": ai_level.value,
                "analysis_id": str(analysis_id),
                "was_fallback": was_fallback,
            },
        )
        candidate.risk_level = final_level
        candidate.risk_source = final_source

    return RiskApplication(
        rule_floor_level=floor.level,
        rule_floor_score=floor.score,
        ai_level=ai_level,
        final_level=final_level,
        risk_source=final_source,
        raised_by_ai=raised,
        applied=True,
        note=note,
    )


def assess_risk(
    db: Session,
    candidate_id: uuid.UUID,
    actor: str,
    *,
    provider: LLMProvider | None = None,
    today: date | None = None,
) -> tuple[Candidate, AIResult[RiskAssessment], RiskApplication]:
    candidate = _get_candidate(db, candidate_id)
    today = today or date.today()

    result = engine.assess_risk(db, candidate, provider=provider, today=today)
    application = _apply_risk_assessment(
        db,
        candidate,
        RiskLevel(result.output.risk_level),
        analysis_id=result.analysis_id,
        was_fallback=result.was_fallback,
        actor=actor,
        today=today,
    )

    # One commit: the analysis row, the risk change and the audit row land
    # together or not at all.
    db.commit()
    db.refresh(candidate)
    return candidate, result, application


# ---------------------------------------------------------------------------
# The read-only analyses
# ---------------------------------------------------------------------------


def summarize(
    db: Session,
    candidate_id: uuid.UUID,
    *,
    provider: LLMProvider | None = None,
    today: date | None = None,
) -> AIResult[InteractionSummary]:
    candidate = _get_candidate(db, candidate_id)
    result = engine.summarize_interactions(db, candidate, provider=provider, today=today)
    db.commit()
    return result


def recommend_action(
    db: Session,
    candidate_id: uuid.UUID,
    *,
    provider: LLMProvider | None = None,
    today: date | None = None,
) -> AIResult[NextAction]:
    candidate = _get_candidate(db, candidate_id)
    result = engine.recommend_next_action(db, candidate, provider=provider, today=today)
    db.commit()
    return result


def draft_message(
    db: Session,
    candidate_id: uuid.UUID,
    *,
    channel: str,
    intent: str,
    tone: str,
    provider: LLMProvider | None = None,
    today: date | None = None,
) -> AIResult[DraftedMessage]:
    candidate = _get_candidate(db, candidate_id)
    result = engine.draft_message(
        db,
        candidate,
        channel=channel,
        intent=intent,
        tone=tone,
        provider=provider,
        today=today,
    )
    db.commit()
    return result


# ---------------------------------------------------------------------------
# HR override
# ---------------------------------------------------------------------------


def _latest_analysis(db: Session, candidate_id: uuid.UUID, analysis_type: str) -> AIAnalysis | None:
    return db.scalar(
        select(AIAnalysis)
        .where(AIAnalysis.candidate_id == candidate_id, AIAnalysis.analysis_type == analysis_type)
        .order_by(AIAnalysis.created_at.desc())
        .limit(1)
    )


def override(
    db: Session, candidate_id: uuid.UUID, payload: AIOverrideRequest, actor: str
) -> tuple[Candidate, list[str]]:
    """Record that a human disagreed with the AI.

    Two things can be overridden, independently or together: the risk level
    (which changes the candidate record and sets risk_source='hr_override')
    and the recommended action (which changes nothing but is written to the
    audit log).

    The second one earns its place despite mutating nothing. There is no
    ground truth for any of this — nobody labels which candidates were about
    to drop out — so a recruiter marking a recommendation as wrong is the only
    correction signal the system will ever get. Discarding it because it has
    no column to live in would throw away the one measurement available.

    Returns the candidate and a human-readable list of what was recorded.
    """
    candidate = _get_candidate(db, candidate_id)
    recorded: list[str] = []

    if payload.risk_level is not None:
        latest = _latest_analysis(db, candidate_id, engine.ANALYSIS_RISK)
        before = {
            "risk_level": candidate.risk_level.value,
            "risk_source": candidate.risk_source.value,
        }
        candidate.risk_level = payload.risk_level
        candidate.risk_source = RiskSource.HR_OVERRIDE

        audit_service.record(
            db,
            entity_type="candidate",
            entity_id=candidate.id,
            action="ai_risk_override",
            actor=actor,
            before=before,
            after={
                "risk_level": payload.risk_level.value,
                "risk_source": RiskSource.HR_OVERRIDE.value,
                "reason": payload.reason,
                "overridden_ai_level": (
                    latest.risk_level.value if latest and latest.risk_level else None
                ),
                "overridden_analysis_id": str(latest.id) if latest else None,
            },
        )
        recorded.append(
            f"risk level overridden to '{payload.risk_level.value}' (risk_source is now hr_override)"
        )

    if payload.recommendation_verdict is not None:
        latest = _latest_analysis(db, candidate_id, engine.ANALYSIS_NEXT_ACTION)
        if latest is None:
            raise NotFoundError(
                f"Candidate {candidate_id} has no AI recommendation to override. "
                "Run POST /ai/candidates/{id}/recommend-action first."
            )

        audit_service.record(
            db,
            entity_type="ai_analysis",
            entity_id=latest.id,
            action="ai_recommendation_override",
            actor=actor,
            before={"recommendation": latest.parsed_output},
            after={
                "verdict": payload.recommendation_verdict,
                "reason": payload.reason,
                "candidate_id": str(candidate_id),
            },
        )
        recorded.append(f"AI recommendation marked '{payload.recommendation_verdict}'")

    db.commit()
    db.refresh(candidate)
    return candidate, recorded
