"""Pydantic request/response models for every Module 2 endpoint.

Create schemas validate input at the system boundary. Update schemas have
every field optional so PATCH can apply a partial diff via
`model_dump(exclude_unset=True)`. Response schemas mirror the ORM models via
`from_attributes=True` and are never re-validated harder than the data
already stored (e.g. plain `str` for email on responses, `EmailStr` only on
create).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.ai.contracts import DraftedMessage, InteractionSummary, NextAction, RiskAssessment
from app.enums import (
    BlockerCategory,
    EngagementStatus,
    FinalOutcome,
    FollowUpPriority,
    FollowUpSource,
    FollowUpStatus,
    InteractionChannel,
    InteractionDirection,
    RecruiterRead,
    RiskLevel,
    RiskSource,
    StageAnchor,
    StageStatus,
    ValidationStatus,
)

# ---------------------------------------------------------------------------
# Recruiters
# ---------------------------------------------------------------------------


class RecruiterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    created_at: datetime
    updated_at: datetime


class RecruiterListResponse(BaseModel):
    items: list[RecruiterResponse]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


class CandidateCreate(BaseModel):
    name: str = Field(min_length=1)
    email: EmailStr
    phone: str | None = None
    role: str = Field(min_length=1)
    department: str = Field(min_length=1)
    location: str = Field(min_length=1)
    offer_date: date
    joining_date: date
    recruiter_id: uuid.UUID
    notes: str | None = None

    @model_validator(mode="after")
    def _joining_not_before_offer(self) -> "CandidateCreate":
        if self.joining_date < self.offer_date:
            raise ValueError("joining_date cannot be before offer_date")
        return self


# Fields that are NOT NULL in the database. A client may omit them from a
# PATCH body (that's how partial update works) but must not explicitly send
# `null` for one of these — that would otherwise surface as a raw DB
# IntegrityError instead of a clean 422.
_CANDIDATE_NON_NULLABLE_FIELDS = {
    "name",
    "email",
    "role",
    "department",
    "location",
    "offer_date",
    "joining_date",
    "recruiter_id",
    "engagement_status",
    "risk_level",
    "final_outcome",
}


class CandidateUpdate(BaseModel):
    name: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    role: str | None = None
    department: str | None = None
    location: str | None = None
    offer_date: date | None = None
    joining_date: date | None = None
    recruiter_id: uuid.UUID | None = None
    engagement_status: EngagementStatus | None = None
    risk_level: RiskLevel | None = None
    notes: str | None = None
    final_outcome: FinalOutcome | None = None

    @model_validator(mode="after")
    def _reject_explicit_null_for_required_fields(self) -> "CandidateUpdate":
        for field in _CANDIDATE_NON_NULLABLE_FIELDS & self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be set to null")
        return self

    @model_validator(mode="after")
    def _joining_not_before_offer(self) -> "CandidateUpdate":
        if (
            "offer_date" in self.model_fields_set
            and "joining_date" in self.model_fields_set
            and self.offer_date is not None
            and self.joining_date is not None
            and self.joining_date < self.offer_date
        ):
            raise ValueError("joining_date cannot be before offer_date")
        return self


class CandidateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    phone: str | None
    role: str
    department: str
    location: str
    offer_date: date
    joining_date: date
    recruiter_id: uuid.UUID
    engagement_status: EngagementStatus
    last_interaction_at: datetime | None
    risk_level: RiskLevel
    risk_source: RiskSource
    risk_score_base: float
    notes: str | None
    final_outcome: FinalOutcome
    created_at: datetime
    updated_at: datetime


class CandidateListResponse(BaseModel):
    items: list[CandidateResponse]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Candidate stages (flattened with the journey_stages template fields a
# timeline UI needs, since candidate_stages alone doesn't carry key/label/
# sequence_order/anchor)
# ---------------------------------------------------------------------------


class CandidateStageResponse(BaseModel):
    id: uuid.UUID
    candidate_id: uuid.UUID
    stage_id: uuid.UUID
    stage_key: str
    stage_label: str
    sequence_order: int
    anchor: StageAnchor
    due_date: date
    status: StageStatus
    completed_at: datetime | None
    completed_by: str | None


# ---------------------------------------------------------------------------
# Interactions
# ---------------------------------------------------------------------------


class InteractionCreate(BaseModel):
    channel: InteractionChannel
    direction: InteractionDirection
    content: str = Field(min_length=1)
    occurred_at: datetime | None = None
    blocker_raised: bool = False
    blocker_category: BlockerCategory | None = None
    date_confirmed: bool | None = None
    recruiter_read: RecruiterRead | None = None

    @model_validator(mode="after")
    def _call_note_fields_only_on_calls(self) -> "InteractionCreate":
        has_call_note_fields = (
            self.blocker_category is not None
            or self.date_confirmed is not None
            or self.recruiter_read is not None
            or self.blocker_raised
        )
        if has_call_note_fields and self.channel != InteractionChannel.CALL:
            raise ValueError(
                "blocker_raised/blocker_category/date_confirmed/recruiter_read are "
                "call-note fields and can only be set when channel='call'"
            )
        return self


class InteractionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    candidate_id: uuid.UUID
    channel: InteractionChannel
    direction: InteractionDirection
    content: str
    occurred_at: datetime
    created_by: str
    blocker_raised: bool
    blocker_category: BlockerCategory | None
    date_confirmed: bool | None
    recruiter_read: RecruiterRead | None
    created_at: datetime
    updated_at: datetime


class InteractionListResponse(BaseModel):
    items: list[InteractionResponse]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# AI analyses (read-only here — Module 5 populates this table; Module 2 only
# surfaces the latest row, if any, on the candidate detail response)
# ---------------------------------------------------------------------------


class AIAnalysisResponse(BaseModel):
    # protected_namespaces=() because model_name is the real column name from
    # CLAUDE.md's ai_analyses table, not something worth renaming just to
    # dodge Pydantic's default "model_" prefix reservation.
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: uuid.UUID
    candidate_id: uuid.UUID
    analysis_type: str
    model_name: str
    prompt_version: str
    parsed_output: dict
    risk_level: RiskLevel | None
    confidence: float
    validation_status: ValidationStatus
    latency_ms: int
    was_fallback: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# Follow-up actions
# ---------------------------------------------------------------------------

_FOLLOW_UP_NON_NULLABLE_FIELDS = {"title", "priority", "status"}


class FollowUpActionUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    due_date: date | None = None
    priority: FollowUpPriority | None = None
    status: FollowUpStatus | None = None

    @model_validator(mode="after")
    def _reject_explicit_null_for_required_fields(self) -> "FollowUpActionUpdate":
        for field in _FOLLOW_UP_NON_NULLABLE_FIELDS & self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be set to null")
        return self


class FollowUpActionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    candidate_id: uuid.UUID
    title: str
    description: str | None
    due_date: date | None
    priority: FollowUpPriority
    status: FollowUpStatus
    source: FollowUpSource
    generated_message: str | None
    rule_key: str | None
    created_at: datetime
    completed_at: datetime | None


class FollowUpActionListResponse(BaseModel):
    items: list[FollowUpActionResponse]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Risk engine (Module 4)
# ---------------------------------------------------------------------------


class RiskRecomputeResponse(BaseModel):
    scanned: int
    score_updated: int
    level_changed: int
    skipped_hr_override: int
    skipped_ai_higher: int
    distribution: dict[str, int]


# ---------------------------------------------------------------------------
# AI service (Module 5)
#
# Every AI response is {meta, <payload>}: the contract the model produced,
# plus how it was produced. `meta` is not diagnostics — was_fallback and
# validation_status decide whether the UI presents an answer as an AI reading
# or as "the provider was down, here is what the rules alone can tell you",
# and a UI that cannot tell those apart will eventually show a recruiter a
# rule floor labelled as an AI insight.
# ---------------------------------------------------------------------------


class AIAnalysisMeta(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    analysis_id: uuid.UUID
    analysis_type: str
    model_name: str
    prompt_version: str
    validation_status: ValidationStatus
    was_fallback: bool
    latency_ms: int
    confidence: float
    created_at: datetime


class RiskApplicationResponse(BaseModel):
    """How the AI's level interacted with the rule floor. `final = max(base, ai)`
    made visible, so the UI can show the badge and its provenance together."""

    rule_floor_level: RiskLevel
    rule_floor_score: float
    ai_level: RiskLevel
    final_level: RiskLevel
    risk_source: RiskSource
    raised_by_ai: bool
    applied: bool
    note: str


class AssessRiskResponse(BaseModel):
    meta: AIAnalysisMeta
    assessment: RiskAssessment
    risk: RiskApplicationResponse


class SummarizeResponse(BaseModel):
    meta: AIAnalysisMeta
    summary: InteractionSummary


class RecommendActionResponse(BaseModel):
    meta: AIAnalysisMeta
    recommendation: NextAction


class DraftMessageRequest(BaseModel):
    channel: Literal["email", "whatsapp"]
    intent: str = Field(min_length=1, max_length=500)
    tone: Literal["warm", "formal", "casual"] = "warm"


class DraftMessageResponse(BaseModel):
    meta: AIAnalysisMeta
    draft: DraftedMessage
    # Anything the guardrails stripped, shown rather than silently dropped: a
    # recruiter about to send this should know the model tried to promise
    # something and was stopped.
    guardrails_removed: list[str]


class AIOverrideRequest(BaseModel):
    """HR disagrees with the AI. Either target may be set, or both."""

    risk_level: RiskLevel | None = None
    recommendation_verdict: Literal["accepted", "rejected"] | None = None
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _at_least_one_target(self) -> "AIOverrideRequest":
        if self.risk_level is None and self.recommendation_verdict is None:
            raise ValueError(
                "an override must set risk_level, recommendation_verdict, or both"
            )
        return self


class AIOverrideResponse(BaseModel):
    candidate: "CandidateResponse"
    recorded: list[str]


# ---------------------------------------------------------------------------
# Candidate detail (GET /candidates/{id}) — candidate + stages + interactions
# + latest AI analysis + open actions, per CLAUDE.md Module 2.
# ---------------------------------------------------------------------------


class CandidateDetailResponse(CandidateResponse):
    stages: list[CandidateStageResponse]
    interactions: list[InteractionResponse]
    latest_ai_analysis: AIAnalysisResponse | None
    open_actions: list[FollowUpActionResponse]
