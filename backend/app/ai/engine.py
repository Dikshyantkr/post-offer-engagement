"""The AI pipeline: prompt -> provider -> validate -> repair -> fallback -> persist.

Four public functions, one shared runner. The runner is where the module earns
its keep, because the interesting part of an LLM feature is not the happy
path — it is what the application does on the two or three percent of calls
that come back wrong, and how much of that is visible afterwards.

The rules, in order:

1. Ask the provider for JSON in structured-output mode.
2. Parse into the Pydantic contract. Success -> validation_status='valid'.
3. On a validation failure, retry EXACTLY ONCE, with the model's own output
   and the exact validator error appended as a repair instruction. Success ->
   validation_status='repaired'.
4. On a second validation failure, or on any provider exception at all,
   return a deterministic rule-based answer: validation_status='failed',
   was_fallback=True, HTTP 200. The caller always gets a usable object of the
   right type. A recruiter's morning does not stop because a model returned a
   trailing comma.
5. Persist the outcome to ai_analyses either way, including every attempt's
   raw text.

A provider exception does NOT earn the repair retry. The repair prompt exists
to fix a shape the model got wrong; a transport or auth failure is not a shape
problem, and a second call would just spend another few seconds failing the
same way while a recruiter waits.

Nothing here commits. The ai_analyses row is flushed into the caller's
transaction so it lands atomically with whatever the caller does next —
in practice, the risk change in ai_service.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Generic, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.ai import guardrails, prompts
from app.ai.contracts import (
    DraftedMessage,
    InteractionSummary,
    NextAction,
    RiskAssessment,
    json_schema_for,
)
from app.ai.provider import LLMProvider, get_provider
from app.enums import (
    BlockerCategory,
    InteractionChannel,
    InteractionDirection,
    RiskLevel,
    StageStatus,
    ValidationStatus,
)
from app.models import AIAnalysis, Candidate, CandidateStage, Interaction
from app.services import risk_service
from app.services.risk_service import RuleFloor

logger = logging.getLogger(__name__)

ContractT = TypeVar("ContractT", bound=BaseModel)

ANALYSIS_RISK = "risk_assessment"
ANALYSIS_SUMMARY = "interaction_summary"
ANALYSIS_NEXT_ACTION = "next_action"
ANALYSIS_DRAFT = "drafted_message"

# ai_analyses.confidence is NOT NULL for every row, but only RiskAssessment
# carries a model-reported confidence. For the other three the column records
# how much the pipeline trusts the row instead: a clean first-pass parse, a
# repaired one, or a deterministic fallback. Documented here because a number
# in a column that means two different things is otherwise a trap.
_PIPELINE_CONFIDENCE = {
    ValidationStatus.VALID: 1.0,
    ValidationStatus.REPAIRED: 0.7,
    ValidationStatus.FAILED: 0.2,
}

# What the deterministic risk fallback claims for itself. Low on purpose: it
# is the rule floor with no message content read at all, and the UI should
# show it as the weak signal it is.
_FALLBACK_RISK_CONFIDENCE = 0.25


@dataclass(frozen=True)
class AIResult(Generic[ContractT]):
    """One completed analysis: the parsed contract plus everything about how
    it was produced. The provenance travels with the value so a caller — and
    ultimately a recruiter looking at the UI — can always tell an LLM answer
    from a rule-based one."""

    output: ContractT
    analysis_id: uuid.UUID
    analysis_type: str
    model_name: str
    prompt_version: str
    validation_status: ValidationStatus
    was_fallback: bool
    latency_ms: int
    confidence: float
    created_at: datetime
    guardrails_removed: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The shared runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Attempt:
    """One provider call. `succeeded` means the call returned text, not that
    the text validated — a response that came back and then failed the
    contract is still the model's own words and belongs in raw_response as
    such."""

    label: str
    text: str
    succeeded: bool


def _format_attempts(attempts: list[_Attempt]) -> str:
    """Every attempt's raw text, kept in raw_response.

    One ai_analyses row per engine call, not per provider call: parsed_output
    is NOT NULL, and a rejected attempt has nothing to put in it. Keeping the
    rejected text here means nothing is lost — the row still answers "what did
    the model actually say, and how many tries did this take", which is the
    question the attempt trail exists to answer.

    The single clean-first-try case — the overwhelming majority — is stored
    bare, so raw_response is exactly what the model returned and stays
    diffable against parsed_output. Anything else gets labelled sections,
    including a lone provider error, where the label is the only thing
    distinguishing a stack trace from a model response.
    """
    if len(attempts) == 1 and attempts[0].succeeded:
        return attempts[0].text
    return "\n\n".join(f"--- {a.label} ---\n{a.text}" for a in attempts)


def _run_analysis(
    db: Session,
    candidate: Candidate,
    *,
    analysis_type: str,
    contract: type[ContractT],
    prompt: str,
    build_fallback: Callable[[], ContractT],
    provider: LLMProvider,
) -> tuple[ContractT, AIAnalysis, list[str]]:
    schema = json_schema_for(contract)
    attempts: list[_Attempt] = []
    latency_ms = 0
    output: ContractT | None = None
    status = ValidationStatus.FAILED
    current_prompt = prompt

    for attempt in (1, 2):
        started = time.perf_counter()
        try:
            raw, provider_latency_ms = provider.generate_json(current_prompt, schema)
            latency_ms += provider_latency_ms
        except Exception as exc:
            # Broad by design: a provider that raises anything at all must
            # degrade to the fallback rather than surface a 500. The provider
            # contract says LLMProviderError, but a third-party SDK breaking
            # that contract should still not take the endpoint down.
            latency_ms += int((time.perf_counter() - started) * 1000)
            logger.warning(
                "AI provider call failed (%s, attempt %d) for candidate %s: %s",
                analysis_type,
                attempt,
                candidate.id,
                exc,
            )
            attempts.append(
                _Attempt(
                    label=f"attempt {attempt}: provider error",
                    text=f"{type(exc).__name__}: {exc}",
                    succeeded=False,
                )
            )
            break

        attempts.append(_Attempt(label=f"attempt {attempt}", text=raw, succeeded=True))

        try:
            output = contract.model_validate_json(raw)
            status = ValidationStatus.VALID if attempt == 1 else ValidationStatus.REPAIRED
            break
        except ValidationError as exc:
            # Covers malformed JSON too: model_validate_json raises
            # ValidationError for a syntax error as well as a shape error.
            logger.warning(
                "AI response failed validation (%s, attempt %d) for candidate %s: %s",
                analysis_type,
                attempt,
                candidate.id,
                exc.errors(include_url=False),
            )
            if attempt == 2:
                break
            current_prompt = prompts.repair_prompt(prompt, raw, str(exc))

    was_fallback = output is None
    if output is None:
        output = build_fallback()
        status = ValidationStatus.FAILED
        logger.warning(
            "AI %s for candidate %s fell back to the deterministic path",
            analysis_type,
            candidate.id,
        )

    removed: list[str] = []
    if isinstance(output, DraftedMessage):
        output, removed, gutted = guardrails.scrub_drafted_message(
            output, fallback_subject=_default_subject(candidate)
        )
        if gutted:
            # What comes back is no longer the model's message, so it is not
            # reported as one.
            output = _fallback_draft(candidate, output.channel, output.tone)
            was_fallback = True
            status = ValidationStatus.FAILED

    confidence = (
        output.confidence
        if isinstance(output, RiskAssessment)
        else _PIPELINE_CONFIDENCE[status]
    )

    analysis = AIAnalysis(
        candidate_id=candidate.id,
        analysis_type=analysis_type,
        model_name=provider.model_name,
        prompt_version=prompts.PROMPT_VERSION,
        raw_response=_format_attempts(attempts) if attempts else "",
        parsed_output=output.model_dump(mode="json"),
        risk_level=RiskLevel(output.risk_level) if isinstance(output, RiskAssessment) else None,
        confidence=confidence,
        validation_status=status,
        latency_ms=latency_ms,
        was_fallback=was_fallback,
    )
    db.add(analysis)
    db.flush()

    return output, analysis, removed


def _to_result(
    output: ContractT, analysis: AIAnalysis, removed: list[str]
) -> AIResult[ContractT]:
    return AIResult(
        output=output,
        analysis_id=analysis.id,
        analysis_type=analysis.analysis_type,
        model_name=analysis.model_name,
        prompt_version=analysis.prompt_version,
        validation_status=analysis.validation_status,
        was_fallback=analysis.was_fallback,
        latency_ms=analysis.latency_ms,
        confidence=analysis.confidence,
        created_at=analysis.created_at,
        guardrails_removed=removed,
    )


# ---------------------------------------------------------------------------
# Context loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Context:
    stages: list[CandidateStage]
    interactions: list[Interaction]
    floor: RuleFloor
    today: date
    rendered: str


def _load_context(db: Session, candidate: Candidate, today: date) -> _Context:
    stages = list(
        db.scalars(
            select(CandidateStage)
            .where(CandidateStage.candidate_id == candidate.id)
            .options(selectinload(CandidateStage.stage))
        )
    )
    # The full history here, not the 30-day slice the rule engine uses: the
    # rules only care about live blocker signals, whereas the model is reading
    # a relationship over time and an old message can still be the one that
    # explains the current silence. prompts.py caps how many reach the prompt.
    interactions = list(
        db.scalars(
            select(Interaction)
            .where(Interaction.candidate_id == candidate.id)
            .order_by(Interaction.occurred_at)
        )
    )
    # Recent-only for the rule floor, matching risk_service's own window.
    cutoff = risk_service.recent_interactions_cutoff(today)
    recent = [i for i in interactions if i.occurred_at >= cutoff]
    floor = risk_service.rule_floor(candidate, stages, recent, today)

    return _Context(
        stages=stages,
        interactions=interactions,
        floor=floor,
        today=today,
        rendered=prompts.build_candidate_context(candidate, stages, interactions, floor, today),
    )


# ---------------------------------------------------------------------------
# Deterministic fallbacks
#
# Every one of these is reachable with the provider switched off entirely, and
# each returns a valid contract instance. They are deliberately plain: their
# job is to keep the product working and be honestly labelled, not to imitate
# the model.
# ---------------------------------------------------------------------------


def _latest_inbound(interactions: list[Interaction]) -> Interaction | None:
    inbound = [i for i in interactions if i.direction == InteractionDirection.INBOUND]
    return max(inbound, key=lambda i: i.occurred_at, default=None)


def _logged_blocker_category(interactions: list[Interaction]) -> str:
    """The most recent blocker a recruiter tagged on a call note.

    Deterministic and human-supplied — no message content is read — which is
    exactly why it is the only concern the fallback is willing to name.
    """
    tagged = [
        i
        for i in interactions
        if i.blocker_raised
        and i.blocker_category is not None
        and i.blocker_category != BlockerCategory.NONE
    ]
    latest = max(tagged, key=lambda i: i.occurred_at, default=None)
    return latest.blocker_category.value if latest is not None else "none"


def _fallback_risk(candidate: Candidate, ctx: _Context) -> RiskAssessment:
    floor = ctx.floor
    signals: list[str] = []

    if candidate.last_interaction_at is None:
        signals.append(f"Never contacted since the offer {floor.days_since_contact} days ago")
    else:
        signals.append(f"No contact for {floor.days_since_contact} days")

    if floor.days_to_joining <= 14:
        signals.append(f"Joining date is {floor.days_to_joining} days away")
    if floor.max_stage_overdue_days > 0:
        signals.append(f"An onboarding stage is {floor.max_stage_overdue_days} days overdue")
    if floor.blocker_signal.value != "none":
        signals.append(f"Recruiter logged a '{floor.blocker_signal.value}' blocker signal on a call")

    return RiskAssessment(
        risk_level=floor.level.value,
        confidence=_FALLBACK_RISK_CONFIDENCE,
        signals=signals[:5],
        reasoning=(
            "AI assessment unavailable — this is the deterministic rule floor, computed from "
            "dates and the recruiter's own call notes only. No message content was read, so a "
            "candidate who replies promptly while quietly leaving would not be caught here. "
            "Treat it as a lower bound and re-run the assessment."
        ),
        concern_category=_logged_blocker_category(ctx.interactions),
    )


def _fallback_summary(candidate: Candidate, ctx: _Context) -> InteractionSummary:
    interactions = ctx.interactions
    if not interactions:
        return InteractionSummary(
            summary=(
                f"AI summary unavailable. {candidate.name} has no logged interactions at all — "
                f"the offer was accepted {ctx.floor.days_since_contact} days ago and nobody has "
                "been in touch since."
            ),
            key_concerns=[],
            sentiment="neutral",
            unresolved_items=["No contact has ever been logged for this candidate"],
        )

    inbound = [i for i in interactions if i.direction == InteractionDirection.INBOUND]
    channels = sorted({i.channel.value for i in interactions})
    last_inbound = _latest_inbound(interactions)
    last_inbound_line = (
        f' Their last message, on {last_inbound.occurred_at.date().isoformat()}: '
        f'"{last_inbound.content.strip()[:200]}"'
        if last_inbound is not None
        else " The candidate has never written in; every logged interaction is outbound."
    )

    summary = (
        f"AI summary unavailable — deterministic fallback. {len(interactions)} interactions "
        f"logged over {', '.join(channels)}, {len(inbound)} of them from "
        f"{candidate.name}. Last contact of any kind was {ctx.floor.days_since_contact} days ago."
        f"{last_inbound_line}"
    )

    concern = _logged_blocker_category(interactions)
    overdue = [
        cs.stage.label
        for cs in ctx.stages
        if cs.status in (StageStatus.PENDING, StageStatus.IN_PROGRESS) and cs.due_date < ctx.today
    ]

    return InteractionSummary(
        summary=summary[:800],
        key_concerns=[] if concern == "none" else [f"Recruiter logged a {concern} blocker on a call"],
        sentiment="neutral",
        unresolved_items=[f"{label} is past its due date" for label in overdue],
    )


def _fallback_next_action(candidate: Candidate, ctx: _Context) -> NextAction:
    floor = ctx.floor
    preferred_channel = "email"
    last_inbound = _latest_inbound(ctx.interactions)
    if last_inbound is not None and last_inbound.channel in (
        InteractionChannel.EMAIL,
        InteractionChannel.WHATSAPP,
    ):
        preferred_channel = last_inbound.channel.value

    if floor.level == RiskLevel.HIGH:
        return NextAction(
            action_type="schedule_call",
            channel="call",
            urgency="high",
            rationale=(
                "AI recommendation unavailable — deterministic fallback. The rule engine has this "
                f"candidate at high risk: {floor.days_since_contact} days without contact and "
                f"{floor.days_to_joining} days to the joining date. A call is the default because "
                "anything a candidate is weighing up needs a conversation, not a message."
            ),
            suggested_timing_days=0,
        )

    if floor.level == RiskLevel.MEDIUM:
        return NextAction(
            action_type="send_message",
            channel=preferred_channel,
            urgency="medium",
            rationale=(
                "AI recommendation unavailable — deterministic fallback. The rule engine has this "
                f"candidate at medium risk ({floor.days_since_contact} days since contact, "
                f"{floor.days_to_joining} days to joining). A check-in on the channel they last "
                "replied on is the low-cost move."
            ),
            suggested_timing_days=1,
        )

    return NextAction(
        action_type="no_action_needed",
        channel=preferred_channel,
        urgency="low",
        rationale=(
            "AI recommendation unavailable — deterministic fallback. The rule engine sees nothing "
            f"pressing: last contact {floor.days_since_contact} days ago, joining in "
            f"{floor.days_to_joining} days, no overdue stages. Note the rules cannot read message "
            "content, so re-run the assessment when the provider is back."
        ),
        suggested_timing_days=7,
    )


def _default_subject(candidate: Candidate) -> str:
    return f"Checking in ahead of your start date, {candidate.name.split()[0]}"


def _fallback_draft(candidate: Candidate, channel: str, tone: str) -> DraftedMessage:
    """A plain, safe, obviously-human-editable template.

    It promises nothing, mentions no numbers and proposes no date change, so
    it needs no scrubbing. The recruiter is expected to rewrite it — which is
    true of every drafted message here, fallback or not.
    """
    first_name = candidate.name.split()[0]
    body = (
        f"Hi {first_name},\n\n"
        f"Hope things are going well at your end. I wanted to check in ahead of your start date "
        f"on {candidate.joining_date.strftime('%d %B')} and see how everything is going with your "
        f"notice period.\n\n"
        f"If anything has come up, or there is anything you would like to talk through before you "
        f"join, do let me know and we can find a time to speak."
    )
    return DraftedMessage(
        channel=channel,
        subject=_default_subject(candidate) if channel == "email" else None,
        body=body,
        tone=tone,
        personalization_used=[
            "candidate's first name",
            "joining date",
            f"role: {candidate.role}",
        ],
    )


# ---------------------------------------------------------------------------
# The four public functions
# ---------------------------------------------------------------------------


def _resolve(provider: LLMProvider | None, today: date | None) -> tuple[LLMProvider, date]:
    return (provider or get_provider()), (today or date.today())


def assess_risk(
    db: Session,
    candidate: Candidate,
    *,
    provider: LLMProvider | None = None,
    today: date | None = None,
) -> AIResult[RiskAssessment]:
    """Read the candidate's own messages and judge whether they will show up.

    This is the half of the risk engine the rules cannot do: a candidate who
    replies within the hour, politely, while their current employer talks them
    out of leaving is invisible to every date-based rule and plainly visible in
    what they wrote. Applying the result to the candidate record — where the
    AI may only ever raise the level — is ai_service's job, not this one's.
    """
    provider, today = _resolve(provider, today)
    ctx = _load_context(db, candidate, today)

    output, analysis, removed = _run_analysis(
        db,
        candidate,
        analysis_type=ANALYSIS_RISK,
        contract=RiskAssessment,
        prompt=prompts.risk_assessment_prompt(ctx.rendered),
        build_fallback=lambda: _fallback_risk(candidate, ctx),
        provider=provider,
    )
    return _to_result(output, analysis, removed)


def summarize_interactions(
    db: Session,
    candidate: Candidate,
    *,
    provider: LLMProvider | None = None,
    today: date | None = None,
) -> AIResult[InteractionSummary]:
    provider, today = _resolve(provider, today)
    ctx = _load_context(db, candidate, today)

    output, analysis, removed = _run_analysis(
        db,
        candidate,
        analysis_type=ANALYSIS_SUMMARY,
        contract=InteractionSummary,
        prompt=prompts.interaction_summary_prompt(ctx.rendered),
        build_fallback=lambda: _fallback_summary(candidate, ctx),
        provider=provider,
    )
    return _to_result(output, analysis, removed)


def recommend_next_action(
    db: Session,
    candidate: Candidate,
    *,
    provider: LLMProvider | None = None,
    today: date | None = None,
) -> AIResult[NextAction]:
    provider, today = _resolve(provider, today)
    ctx = _load_context(db, candidate, today)

    output, analysis, removed = _run_analysis(
        db,
        candidate,
        analysis_type=ANALYSIS_NEXT_ACTION,
        contract=NextAction,
        prompt=prompts.next_action_prompt(ctx.rendered),
        build_fallback=lambda: _fallback_next_action(candidate, ctx),
        provider=provider,
    )
    return _to_result(output, analysis, removed)


def draft_message(
    db: Session,
    candidate: Candidate,
    *,
    channel: str,
    intent: str,
    tone: str = "warm",
    provider: LLMProvider | None = None,
    today: date | None = None,
) -> AIResult[DraftedMessage]:
    """Draft a message for the recruiter to edit and send.

    The AI never contacts the candidate. What comes back is a starting point
    that a human rewrites — which is why the guardrail scrub runs on this
    output and on nothing else in the module.
    """
    provider, today = _resolve(provider, today)
    ctx = _load_context(db, candidate, today)

    output, analysis, removed = _run_analysis(
        db,
        candidate,
        analysis_type=ANALYSIS_DRAFT,
        contract=DraftedMessage,
        prompt=prompts.draft_message_prompt(ctx.rendered, channel, intent, tone),
        build_fallback=lambda: _fallback_draft(candidate, channel, tone),
        provider=provider,
    )
    return _to_result(output, analysis, removed)
