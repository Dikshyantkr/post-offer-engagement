import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db import Base
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


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Recruiter(TimestampMixin, Base):
    __tablename__ = "recruiters"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    candidates: Mapped[list["Candidate"]] = relationship(back_populates="recruiter")


class Candidate(TimestampMixin, Base):
    __tablename__ = "candidates"
    __table_args__ = (
        Index("ix_candidates_joining_date", "joining_date"),
        Index("ix_candidates_risk_level", "risk_level"),
        Index("ix_candidates_recruiter_id", "recruiter_id"),
        Index("ix_candidates_engagement_status", "engagement_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str] = mapped_column(Text, nullable=False)
    offer_date: Mapped[date] = mapped_column(Date, nullable=False)
    joining_date: Mapped[date] = mapped_column(Date, nullable=False)
    recruiter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("recruiters.id"), nullable=False
    )
    engagement_status: Mapped[EngagementStatus] = mapped_column(
        nullable=False, default=EngagementStatus.OFFER_ACCEPTED
    )
    last_interaction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    risk_level: Mapped[RiskLevel] = mapped_column(nullable=False, default=RiskLevel.LOW)
    risk_source: Mapped[RiskSource] = mapped_column(nullable=False, default=RiskSource.RULE)
    risk_score_base: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    notes: Mapped[str | None] = mapped_column(Text)
    final_outcome: Mapped[FinalOutcome] = mapped_column(nullable=False, default=FinalOutcome.PENDING)

    recruiter: Mapped["Recruiter"] = relationship(back_populates="candidates")
    stages: Mapped[list["CandidateStage"]] = relationship(back_populates="candidate")
    interactions: Mapped[list["Interaction"]] = relationship(back_populates="candidate")
    ai_analyses: Mapped[list["AIAnalysis"]] = relationship(back_populates="candidate")
    follow_up_actions: Mapped[list["FollowUpAction"]] = relationship(back_populates="candidate")


class JourneyStage(TimestampMixin, Base):
    """The workflow template. Seeded, not hardcoded."""

    __tablename__ = "journey_stages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    sequence_order: Mapped[int] = mapped_column(Integer, nullable=False)
    anchor: Mapped[StageAnchor] = mapped_column(nullable=False)
    offset_days: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    candidate_stages: Mapped[list["CandidateStage"]] = relationship(back_populates="stage")


class CandidateStage(TimestampMixin, Base):
    """Materialised per candidate on creation."""

    __tablename__ = "candidate_stages"
    __table_args__ = (
        Index("ix_candidate_stage_unique", "candidate_id", "stage_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidates.id"), nullable=False
    )
    stage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("journey_stages.id"), nullable=False
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[StageStatus] = mapped_column(nullable=False, default=StageStatus.PENDING)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by: Mapped[str | None] = mapped_column(Text)

    candidate: Mapped["Candidate"] = relationship(back_populates="stages")
    stage: Mapped["JourneyStage"] = relationship(back_populates="candidate_stages")


class Interaction(TimestampMixin, Base):
    __tablename__ = "interactions"
    __table_args__ = (
        Index("ix_interactions_candidate_occurred", "candidate_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidates.id"), nullable=False
    )
    channel: Mapped[InteractionChannel] = mapped_column(nullable=False)
    direction: Mapped[InteractionDirection] = mapped_column(nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    blocker_raised: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocker_category: Mapped[BlockerCategory | None] = mapped_column(nullable=True)
    date_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    recruiter_read: Mapped[RecruiterRead | None] = mapped_column(nullable=True)

    candidate: Mapped["Candidate"] = relationship(back_populates="interactions")


class AIAnalysis(TimestampMixin, Base):
    """Every call persisted, never just returned."""

    __tablename__ = "ai_analyses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidates.id"), nullable=False
    )
    analysis_type: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    raw_response: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_output: Mapped[dict] = mapped_column(JSONB, nullable=False)
    risk_level: Mapped[RiskLevel | None] = mapped_column(nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    validation_status: Mapped[ValidationStatus] = mapped_column(nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    was_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    candidate: Mapped["Candidate"] = relationship(back_populates="ai_analyses")


class FollowUpAction(TimestampMixin, Base):
    __tablename__ = "follow_up_actions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidates.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    due_date: Mapped[date | None] = mapped_column(Date)
    priority: Mapped[FollowUpPriority] = mapped_column(nullable=False)
    status: Mapped[FollowUpStatus] = mapped_column(nullable=False, default=FollowUpStatus.OPEN)
    source: Mapped[FollowUpSource] = mapped_column(nullable=False)
    generated_message: Mapped[str | None] = mapped_column(Text)
    rule_key: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    candidate: Mapped["Candidate"] = relationship(back_populates="follow_up_actions")


class AuditLog(TimestampMixin, Base):
    """Written on every candidate update, stage transition, and AI override."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
