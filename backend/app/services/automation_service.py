"""Module 6 — the nightly engagement sweep.

Two rules, run together by `run_engagement_sweep()`. Both turn a condition
nobody is watching into a row in the recruiter's action queue.

    imminent_silence   joining within 7 days AND silent for 5+ days (or never
                       contacted). Runs the AI assessment, drafts a message,
                       creates an action carrying that draft.
    stage_stall        an onboarding stage still pending past its due date.
                       Purely factual, no AI — the company is late, and no
                       model is needed to know that.

Three properties this module is built around.

**It never sends anything.** The drafted message is stored on the action and
logged; a recruiter reads, edits and sends it. That is not a limitation of
the demo, it is the product decision in CLAUDE.md's opening paragraph — an
obviously automated nudge to a wavering candidate makes things worse. So
"simulated send" here means the message reaches the recruiter's queue, not
the candidate.

**The AI is optional to it.** Rule 1 calls the AI layer for evidence and a
draft, but a candidate is never skipped because that layer misbehaved. If
the engine falls back, the action is created with the fallback text; if the
whole AI layer raises, the action is created with a deterministic template
written here. A candidate going silent five days before they join is the
single most important thing this system detects, and it must not depend on a
third-party API being up.

**It is safe to run twice.** Both rules skip a candidate that already has an
open action from the same rule inside the idempotency window, checked BEFORE
any AI call so a repeat run costs nothing.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.ai.provider import LLMProvider
from app.enums import (
    FinalOutcome,
    FollowUpPriority,
    FollowUpSource,
    FollowUpStatus,
    InteractionChannel,
    InteractionDirection,
    RiskLevel,
    RiskSource,
    StageStatus,
)
from app.models import Candidate, CandidateStage, FollowUpAction, Interaction
from app.services import ai_service, audit_service, risk_service

logger = logging.getLogger(__name__)

RULE_IMMINENT_SILENCE = "imminent_silence"
RULE_STAGE_STALL = "stage_stall"

# CLAUDE.md's rule 1 thresholds, named so the query reads as the rule.
JOINING_HORIZON_DAYS = 7
SILENCE_DAYS = 5

# "No second open action for the same candidate + rule_key within 24h."
# Deliberately a window rather than "any open action blocks a new one": a
# candidate who is still silent tomorrow has a genuinely worse problem than
# they had today, and the daily cron re-raising them is escalation, not noise.
# An open action older than the window is a nudge nobody acted on.
IDEMPOTENCY_WINDOW_HOURS = 24

# Rule 1 catches candidates who are both silent and about to join, so the
# action is always for today.
ACTION_DUE_TODAY = 0


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@dataclass
class RuleOutcome:
    """Per-rule counters. `matched` counts CANDIDATES, not matching rows — a
    candidate with three overdue stages is one stalled candidate and gets one
    action, because the idempotency key is candidate + rule_key."""

    matched: int = 0
    actions_created: int = 0
    skipped_existing_action: int = 0


@dataclass
class SweepSummary:
    started_at: datetime
    candidates_scanned: int = 0
    imminent_silence: RuleOutcome = field(default_factory=RuleOutcome)
    stage_stall: RuleOutcome = field(default_factory=RuleOutcome)
    ai_calls: int = 0
    ai_fallbacks: int = 0
    messages_simulated: int = 0
    errors: int = 0
    duration_ms: int = 0

    @property
    def actions_created(self) -> int:
        return self.imminent_silence.actions_created + self.stage_stall.actions_created

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "candidates_scanned": self.candidates_scanned,
            "actions_created": self.actions_created,
            "rules": {
                RULE_IMMINENT_SILENCE: vars(self.imminent_silence),
                RULE_STAGE_STALL: vars(self.stage_stall),
            },
            "ai_calls": self.ai_calls,
            "ai_fallbacks": self.ai_fallbacks,
            "messages_simulated": self.messages_simulated,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _has_recent_open_action(
    db: Session, candidate_id: uuid.UUID, rule_key: str, now: datetime
) -> bool:
    """The idempotency guard. Checked before any AI call, so a repeat sweep
    costs no tokens and no latency."""
    cutoff = now - timedelta(hours=IDEMPOTENCY_WINDOW_HOURS)
    existing = db.scalar(
        select(FollowUpAction.id)
        .where(
            FollowUpAction.candidate_id == candidate_id,
            FollowUpAction.rule_key == rule_key,
            FollowUpAction.status == FollowUpStatus.OPEN,
            FollowUpAction.created_at >= cutoff,
        )
        .limit(1)
    )
    return existing is not None


def _record_action(
    db: Session, action: FollowUpAction, rule_key: str, actor: str, extra: dict
) -> None:
    """Persist an action and its audit row together.

    CLAUDE.md scopes audit_log to candidate updates, stage transitions and AI
    overrides; automation writing into a recruiter's queue unprompted belongs
    on that list too. "Where did this action come from?" should be answerable
    from the audit log rather than by reading this file.
    """
    db.add(action)
    db.flush()
    audit_service.record(
        db,
        entity_type="follow_up_action",
        entity_id=action.id,
        action="automation_action_created",
        actor=actor,
        before=None,
        after={
            "rule_key": rule_key,
            "candidate_id": str(action.candidate_id),
            "priority": action.priority.value,
            "title": action.title,
            **extra,
        },
    )


def _simulate_send(candidate: Candidate, channel: str, message: str) -> None:
    """Log the drafted message instead of delivering it.

    No email or WhatsApp provider is wired up anywhere in this codebase, and
    the destination would not be the candidate in any case — the message goes
    to the recruiter's queue for editing. This log line is the record that the
    draft pipeline ran end to end.
    """
    logger.info(
        "[SIMULATED SEND] candidate=%s (%s) channel=%s — draft stored on the follow-up "
        "action for recruiter review; nothing was delivered to the candidate.\n%s",
        candidate.name,
        candidate.id,
        channel,
        message,
    )


# ---------------------------------------------------------------------------
# Rule 1 — imminent_silence
# ---------------------------------------------------------------------------


def _imminent_silence_candidates(db: Session, now: datetime) -> list[Candidate]:
    """CLAUDE.md's rule, transcribed.

    Note there is no lower bound on joining_date: a candidate whose start date
    has already passed while they are still 'pending' is the most alarming
    version of this and must not fall out of the query.
    """
    return list(
        db.scalars(
            select(Candidate).where(
                Candidate.final_outcome == FinalOutcome.PENDING,
                Candidate.joining_date <= now.date() + timedelta(days=JOINING_HORIZON_DAYS),
                or_(
                    Candidate.last_interaction_at <= now - timedelta(days=SILENCE_DAYS),
                    Candidate.last_interaction_at.is_(None),
                ),
            )
        )
    )


def _preferred_channel(db: Session, candidate: Candidate) -> str:
    """Draft on the channel they last actually replied on.

    A candidate who answers WhatsApp and ignores email should not be chased by
    email. Falls back to email for a candidate who has never written in, which
    is the safer default for a first approach.
    """
    latest_inbound = db.scalar(
        select(Interaction)
        .where(
            Interaction.candidate_id == candidate.id,
            Interaction.direction == InteractionDirection.INBOUND,
            Interaction.channel.in_([InteractionChannel.EMAIL, InteractionChannel.WHATSAPP]),
        )
        .order_by(Interaction.occurred_at.desc())
        .limit(1)
    )
    return latest_inbound.channel.value if latest_inbound is not None else "email"


# Rule 1 already guarantees a HIGH rule floor (silent >= 5 days AND joining
# within 7), so in practice this maps HIGH -> URGENT for every candidate the
# rule catches. The lower rows are not dead code: an HR override to low or
# medium survives the assessment, and the queue should respect that judgement
# instead of shouting over it.
_RISK_TO_PRIORITY = {
    RiskLevel.HIGH: FollowUpPriority.URGENT,
    RiskLevel.MEDIUM: FollowUpPriority.HIGH,
    RiskLevel.LOW: FollowUpPriority.MEDIUM,
}


def _priority_for(db: Session, candidate: Candidate, today: date) -> FollowUpPriority:
    """Priority from the candidate's risk level, floored by the rules.

    The floor is not redundant. `candidate.risk_level` is only as fresh as the
    last thing that scored them, and the thing that was supposed to score them
    here is the AI assessment — which may have just failed. A candidate
    created moments ago still carries the LOW column default, so deriving
    priority from that value alone would file an urgent, silent, about-to-join
    candidate as medium precisely when the AI layer is down. That is the
    failure this module exists to prevent, in its quietest form: not missed,
    just buried in the queue.

    An HR override is exempt. A human who has already looked at this candidate
    and called them low risk should not be shouted over by the rules.
    """
    level = candidate.risk_level
    if candidate.risk_source != RiskSource.HR_OVERRIDE:
        floor = risk_service.rule_floor_for_candidate(db, candidate, today)
        if risk_service.is_higher(floor.level, level):
            level = floor.level
    return _RISK_TO_PRIORITY[level]


def _silence_intent(days_silent: int, days_to_joining: int) -> str:
    when = (
        f"in {days_to_joining} days"
        if days_to_joining > 0
        else ("today" if days_to_joining == 0 else f"{abs(days_to_joining)} days ago")
    )
    return (
        f"Re-open contact. They have not been in touch for {days_silent} days and are due "
        f"to join {when}. Ask an open question about how the notice period and handover are "
        "going, and offer a short call this week. Do not ask them to confirm they are still "
        "joining."
    )


def _render_generated_message(draft) -> str:
    """Flatten a DraftedMessage into the single generated_message column.

    The subject is kept inline rather than dropped — a recruiter copying an
    email draft out of the queue needs it, and the contract guarantees it is
    present exactly when the channel is email.
    """
    if draft.subject:
        return f"Subject: {draft.subject}\n\n{draft.body}"
    return draft.body


def _template_message(candidate: Candidate) -> str:
    """Last-resort message, used only when the AI layer raises outright.

    Deliberately written here rather than reused from ai/engine.py: this is
    the path taken when that module is the thing that broke, so it must not
    depend on it. It promises nothing and proposes no change, so it needs no
    guardrail scrub.
    """
    first_name = candidate.name.split()[0]
    return (
        f"Hi {first_name},\n\n"
        f"Just checking in ahead of your start date on "
        f"{candidate.joining_date.strftime('%d %B')}. We have not heard from you in a little "
        f"while and wanted to make sure everything is going smoothly with your notice period.\n\n"
        f"If anything has come up, or there is anything you would like to talk through before "
        f"you join, let me know and we can find a time to speak."
    )


def _silence_description(assessment, days_silent: int, days_to_joining: int) -> str:
    """The recruiter-facing 'why am I looking at this' text.

    The rule's own facts come first and always — they are true whether or not
    the AI answered. The AI's reasoning and quoted evidence are appended when
    available, labelled as a fallback when that is what they are, so the
    recruiter is never shown a rule floor dressed up as an AI reading.
    """
    lines = [
        f"Flagged by the nightly sweep: no contact for {days_silent} days with "
        f"{days_to_joining} days to the joining date."
    ]

    if assessment is None:
        lines.append(
            "\nThe AI layer was unavailable, so no message-content analysis is attached. "
            "The dates alone justify the call."
        )
        return "\n".join(lines)

    label = "Rule-based fallback" if assessment.was_fallback else "AI assessment"
    lines.append(f"\n{label} ({assessment.output.risk_level}): {assessment.output.reasoning}")

    if assessment.output.signals:
        lines.append("\nEvidence:")
        lines.extend(f"  - {signal}" for signal in assessment.output.signals)

    return "\n".join(lines)


def _handle_imminent_silence(
    db: Session,
    candidate: Candidate,
    summary: SweepSummary,
    actor: str,
    provider: LLMProvider | None,
    now: datetime,
) -> None:
    today = now.date()
    days_silent = risk_service.days_since_contact(candidate, today)
    days_to_joining = (candidate.joining_date - today).days
    channel = _preferred_channel(db, candidate)
    candidate_id = candidate.id
    candidate_name = candidate.name

    assessment = None
    draft = None
    try:
        # CLAUDE.md: "raise risk, run assessment". These are one operation —
        # ai_service applies final = max(rule_floor, ai) and audits the change.
        _candidate, assessment, _application = ai_service.assess_risk(
            db, candidate_id, actor, provider=provider, today=today
        )
        summary.ai_calls += 1
        summary.ai_fallbacks += int(assessment.was_fallback)

        draft = ai_service.draft_message(
            db,
            candidate_id,
            channel=channel,
            intent=_silence_intent(days_silent, days_to_joining),
            tone="warm",
            provider=provider,
            today=today,
        )
        summary.ai_calls += 1
        summary.ai_fallbacks += int(draft.was_fallback)
    except Exception:
        # The engine already degrades internally, so reaching here means
        # something below it broke. The candidate is still flagged: a silent
        # joiner must never be missed because an API was rate-limited.
        logger.exception(
            "AI layer failed for candidate %s; creating the action with a template message",
            candidate_id,
        )
        db.rollback()
        summary.errors += 1

    # The assessment above may have changed risk_level; re-read it so the
    # action's priority reflects the level the recruiter will see on the badge.
    candidate = db.get(Candidate, candidate_id)
    priority = _priority_for(db, candidate, today)
    message = (
        _render_generated_message(draft.output)
        if draft is not None
        else _template_message(candidate)
    )

    action = FollowUpAction(
        candidate_id=candidate_id,
        title=(
            f"Call {candidate_name} — silent {days_silent} days, "
            f"joining in {days_to_joining} days"
        ),
        description=_silence_description(assessment, days_silent, days_to_joining),
        due_date=today + timedelta(days=ACTION_DUE_TODAY),
        priority=priority,
        status=FollowUpStatus.OPEN,
        source=FollowUpSource.AUTOMATION,
        generated_message=message,
        rule_key=RULE_IMMINENT_SILENCE,
    )
    _record_action(
        db,
        action,
        RULE_IMMINENT_SILENCE,
        actor,
        {
            "days_silent": days_silent,
            "days_to_joining": days_to_joining,
            "risk_level": candidate.risk_level.value,
            "channel": channel,
            "ai_available": draft is not None,
            "was_fallback": bool(draft is not None and draft.was_fallback),
        },
    )
    db.commit()

    _simulate_send(candidate, channel, message)
    summary.imminent_silence.actions_created += 1
    summary.messages_simulated += 1


def _run_imminent_silence(
    db: Session,
    summary: SweepSummary,
    actor: str,
    provider: LLMProvider | None,
    now: datetime,
) -> None:
    candidates = _imminent_silence_candidates(db, now)
    summary.imminent_silence.matched = len(candidates)

    for candidate in candidates:
        if _has_recent_open_action(db, candidate.id, RULE_IMMINENT_SILENCE, now):
            summary.imminent_silence.skipped_existing_action += 1
            continue
        try:
            _handle_imminent_silence(db, candidate, summary, actor, provider, now)
        except Exception:
            # One candidate must not take the sweep down. Each candidate
            # commits on its own, so the ones already processed stand.
            logger.exception("imminent_silence failed for candidate %s", candidate.id)
            db.rollback()
            summary.errors += 1


# ---------------------------------------------------------------------------
# Rule 2 — stage_stall
# ---------------------------------------------------------------------------


def _stalled_stages(db: Session, today: date) -> list[CandidateStage]:
    """Open stages past their due date, for candidates still pending.

    PENDING only, not IN_PROGRESS — CLAUDE.md's wording, and it is the right
    line: IN_PROGRESS means somebody is already on it, and nudging them is
    noise. (risk_service counts both when scoring stage lag, because for risk
    the question is "is this late?", not "is anyone on it?")
    """
    return list(
        db.scalars(
            select(CandidateStage)
            .join(Candidate, Candidate.id == CandidateStage.candidate_id)
            .where(
                Candidate.final_outcome == FinalOutcome.PENDING,
                CandidateStage.status == StageStatus.PENDING,
                CandidateStage.due_date < today,
            )
            .options(
                selectinload(CandidateStage.stage),
                selectinload(CandidateStage.candidate),
            )
        )
    )


def _stall_priority(days_overdue: int, days_to_joining: int) -> FollowUpPriority:
    """Deterministic. Mostly mirrors the risk engine's stage-lag banding, so
    the queue and the risk badge do not tell a recruiter two different stories.

    The last rule is where it deliberately parts company with the risk engine.
    The badge answers "will they join?", for which a stalled stage on someone
    joining in five weeks barely matters. The queue answers "what do I fix
    today?", and a step nobody has touched in three weeks is an operational
    failure worth ranking above a stage that slipped yesterday — otherwise a
    22-day stall and a 1-day stall sort identically and the queue stops being
    a triage tool.

    The 14-day threshold is the risk engine's own STAGE_LAG_SATURATION_DAYS:
    the point at which it stops counting lag as a matter of degree and treats
    the step as simply not happening.
    """
    if days_to_joining <= 7:
        return FollowUpPriority.URGENT
    if days_overdue > 7 and days_to_joining <= 21:
        return FollowUpPriority.HIGH
    if days_overdue > risk_service.STAGE_LAG_SATURATION_DAYS:
        return FollowUpPriority.HIGH
    return FollowUpPriority.MEDIUM


def _handle_stage_stall(
    db: Session,
    stages: list[CandidateStage],
    summary: SweepSummary,
    actor: str,
    today: date,
) -> None:
    # Most overdue first; sequence_order breaks ties so the earliest step in
    # the journey is the one named, which is also the one blocking the rest.
    stages.sort(key=lambda cs: (cs.due_date, cs.stage.sequence_order))
    worst = stages[0]
    candidate = worst.candidate

    days_overdue = (today - worst.due_date).days
    days_to_joining = (candidate.joining_date - today).days

    description = [
        f"'{worst.stage.label}' was due {worst.due_date.isoformat()} and is still pending "
        f"— {days_overdue} days overdue, with {days_to_joining} days to the joining date.",
        "",
        "This is the company being late, not the candidate. No AI assessment is attached "
        "because none is needed: the stage has a due date and it passed.",
    ]
    if len(stages) > 1:
        description.append("")
        description.append(f"{len(stages)} stages are overdue for this candidate:")
        description.extend(
            f"  - {cs.stage.label} (due {cs.due_date.isoformat()}, "
            f"{(today - cs.due_date).days} days overdue)"
            for cs in stages
        )

    action = FollowUpAction(
        candidate_id=candidate.id,
        title=f"'{worst.stage.label}' overdue {days_overdue} days for {candidate.name}",
        description="\n".join(description),
        due_date=today,
        priority=_stall_priority(days_overdue, days_to_joining),
        status=FollowUpStatus.OPEN,
        source=FollowUpSource.AUTOMATION,
        # No drafted message: the fix is for the company to do the step, not
        # to write to the candidate about it.
        generated_message=None,
        rule_key=RULE_STAGE_STALL,
    )
    _record_action(
        db,
        action,
        RULE_STAGE_STALL,
        actor,
        {
            "stage_key": worst.stage.key,
            "days_overdue": days_overdue,
            "days_to_joining": days_to_joining,
            "overdue_stage_count": len(stages),
        },
    )
    db.commit()
    summary.stage_stall.actions_created += 1


def _run_stage_stall(db: Session, summary: SweepSummary, actor: str, now: datetime) -> None:
    today = now.date()

    by_candidate: dict[uuid.UUID, list[CandidateStage]] = defaultdict(list)
    for candidate_stage in _stalled_stages(db, today):
        by_candidate[candidate_stage.candidate_id].append(candidate_stage)

    summary.stage_stall.matched = len(by_candidate)

    for candidate_id, stages in by_candidate.items():
        if _has_recent_open_action(db, candidate_id, RULE_STAGE_STALL, now):
            summary.stage_stall.skipped_existing_action += 1
            continue
        try:
            _handle_stage_stall(db, stages, summary, actor, today)
        except Exception:
            logger.exception("stage_stall failed for candidate %s", candidate_id)
            db.rollback()
            summary.errors += 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_engagement_sweep(
    db: Session,
    *,
    actor: str = "automation",
    provider: LLMProvider | None = None,
    now: datetime | None = None,
) -> SweepSummary:
    """Run both rules and return what happened.

    `now` is injectable so the sweep's date arithmetic can be tested without
    waiting for a calendar, exactly like compute_base_risk and
    compute_stage_schedule.

    Commits per candidate rather than once at the end: a sweep that dies
    halfway should leave the actions it already created, and a single
    candidate whose AI call blows up should not roll back the rest.
    """
    now = now or datetime.now(timezone.utc)
    started = datetime.now(timezone.utc)
    summary = SweepSummary(started_at=now)

    summary.candidates_scanned = (
        db.scalar(
            select(func.count())
            .select_from(Candidate)
            .where(Candidate.final_outcome == FinalOutcome.PENDING)
        )
        or 0
    )

    logger.info("Engagement sweep starting: %d pending candidates", summary.candidates_scanned)

    _run_imminent_silence(db, summary, actor, provider, now)
    _run_stage_stall(db, summary, actor, now)

    summary.duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    logger.info(
        "Engagement sweep complete: %d actions created (%d imminent_silence, %d stage_stall), "
        "%d AI calls (%d fallbacks), %d errors, %dms",
        summary.actions_created,
        summary.imminent_silence.actions_created,
        summary.stage_stall.actions_created,
        summary.ai_calls,
        summary.ai_fallbacks,
        summary.errors,
        summary.duration_ms,
    )
    return summary
