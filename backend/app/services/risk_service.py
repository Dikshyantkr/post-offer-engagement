"""Deterministic, rule-based risk scoring — the rule floor of Module 4.

No LLM, no prompts, no provider. Every number here is reproducible from the
three inputs, which is the point: these rules keep working when the AI
provider is down, and they catch the failure mode the AI cannot see —
a candidate who has simply gone quiet.

The rules deliberately over-flag. A false positive costs one phone call; a
false negative costs a hire. Silence is treated as a signal even though it
is genuinely ambiguous (busy != leaving), because the asymmetry of those
two costs justifies the noise.

What the rules explicitly do NOT catch: a candidate who replies promptly,
politely, and on time while quietly accepting a competing offer. Nothing in
days_since_contact or stage lag sees that. Module 5's AI reads the content
of their messages and may RAISE the level; it can never lower it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import (
    BlockerCategory,
    BlockerSignal,
    FinalOutcome,
    RecruiterRead,
    RiskLevel,
    RiskSource,
    StageStatus,
)
from app.models import Candidate, CandidateStage, Interaction
from app.services import audit_service

# --- Score banding -----------------------------------------------------------
# Each band owns a contiguous slice of the 0-100 scale.
BAND_RANGES: dict[RiskLevel, tuple[float, float]] = {
    RiskLevel.LOW: (0.0, 39.0),
    RiskLevel.MEDIUM: (40.0, 69.0),
    RiskLevel.HIGH: (70.0, 100.0),
}

_BAND_RANK = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}

# --- Blocker signal ----------------------------------------------------------
BLOCKER_SIGNAL_RANK = {
    BlockerSignal.NONE: 0,
    BlockerSignal.UNSURE: 1,
    BlockerSignal.CONCERN: 2,
    BlockerSignal.CRITICAL: 3,
}

# The band a blocker signal guarantees *at minimum*. It can raise a candidate's
# band, never lower it.
BLOCKER_FLOOR: dict[BlockerSignal, RiskLevel] = {
    BlockerSignal.NONE: RiskLevel.LOW,
    BlockerSignal.UNSURE: RiskLevel.LOW,  # nudge only — no floor
    BlockerSignal.CONCERN: RiskLevel.MEDIUM,
    BlockerSignal.CRITICAL: RiskLevel.HIGH,
}

# Extra raw severity points, so a blocker also ranks a candidate *within* the
# band it lands in. Added on top of the three base components, total capped
# at 100.
BLOCKER_POINTS: dict[BlockerSignal, float] = {
    BlockerSignal.NONE: 0.0,
    BlockerSignal.UNSURE: 8.0,
    BlockerSignal.CONCERN: 15.0,
    BlockerSignal.CRITICAL: 25.0,
}

# A blocker raised in one of these categories is the strongest deterministic
# leaving-signal we have: both mean a concrete competing pull, not a logistics
# problem we can solve for them.
CRITICAL_BLOCKER_CATEGORIES = {
    BlockerCategory.COUNTER_OFFER,
    BlockerCategory.NOTICE_PERIOD,
}

# Signals age out. A concern raised two months ago and since worked through
# should not keep scoring forever. The schema has no explicit "resolved" flag,
# so recency is the proxy for unresolved.
BLOCKER_SIGNAL_WINDOW_DAYS = 30

# --- Score component weights (must sum to 100) -------------------------------
SILENCE_MAX_POINTS = 45.0
IMMINENCE_MAX_POINTS = 35.0
STAGE_LAG_MAX_POINTS = 20.0

# Points saturate at these values; beyond them the component is maxed out.
SILENCE_SATURATION_DAYS = 20
IMMINENCE_HORIZON_DAYS = 30
STAGE_LAG_SATURATION_DAYS = 14


def compute_base_risk(
    days_to_joining: int,
    days_since_contact: int,
    max_stage_overdue_days: int,
    blocker_signal: BlockerSignal = BlockerSignal.NONE,
) -> tuple[float, RiskLevel]:
    """Return (risk_score_base, risk_level) from the three rule inputs.

    Pure: no database, no datetime.now(). Every input is supplied by the
    caller, exactly like compute_stage_schedule, so the result is fully
    determined by its arguments and unit-testable without a database.

    Inputs
    ------
    days_to_joining
        joining_date - today, in days. Negative means the joining date has
        already passed while the candidate is still pending.
    days_since_contact
        Days since last_interaction_at. For a never-contacted candidate the
        caller counts from offer_date (see _days_since_contact).
    max_stage_overdue_days
        Days past due for the most overdue still-open candidate_stages row;
        0 when nothing is overdue.
    blocker_signal
        Most severe structured concern the recruiter logged on a call note
        within the last 30 days, already resolved to a single value by
        resolve_blocker_signal(). Defaults to NONE so callers isolating the
        time-based rules can omit it.

    Banding (decided by rules, never by the score)
    ----------------------------------------------
    HIGH    silent >= 10 days
            OR silent >= 5 days AND joining within 14 days
            OR any stage > 7 days overdue AND joining within 21 days
    MEDIUM  silent >= 7 days
            OR joining within 7 days regardless of contact
            OR any stage overdue at all
    LOW     otherwise

    The blocker signal then applies a *floor* — it can raise the band the
    time-based rules produced, never lower it:

        CRITICAL   floor of HIGH     counter_offer or notice_period blocker
        CONCERN    floor of MEDIUM   any other blocker, or recruiter_read
                                     'worried'
        UNSURE     no floor          recruiter_read 'unsure' — raises the
                                     score within the band, band unchanged
        NONE       no floor

    What the 0-100 score means
    --------------------------
    The score does NOT decide the band — the rules above do. The score
    orders candidates *within* their band, so a recruiter triaging 40
    pending joiners can rank the five worth calling first.

    Step 1, a raw 0-100 severity from three weighted components, plus a
    blocker modifier added on top and the total capped at 100:

        silence     0-45 pts   min(days_since_contact, 20) / 20
        imminence   0-35 pts   how close joining is, ramping from 30 days out
        stage lag   0-20 pts   min(max_stage_overdue_days, 14) / 14
        blocker     +0/8/15/25 NONE / UNSURE / CONCERN / CRITICAL

    The blocker is a modifier rather than a fourth weighted component so a
    candidate with no logged blocker scores exactly as they did before the
    signal existed.

    Step 2, that raw value is mapped *proportionally* into its band's window
    (LOW 0-39, MEDIUM 40-69, HIGH 70-100):

        score = floor + (ceiling - floor) * raw / 100

    Proportional rather than clamped, so two HIGH candidates with different
    severities keep different scores instead of both pinning to 70.0.

    So: **72 means this candidate is in the HIGH band and sits near the
    bottom of it** — worth a call today, but less urgent than an 88. Read
    the band first (should I call them?) and the position within it second
    (who do I call first?). A 39 and a 40 are near-identical situations
    either side of a deliberately conservative cutoff; the band changed,
    the underlying severity barely did.
    """
    days_since_contact = max(0, days_since_contact)
    max_stage_overdue_days = max(0, max_stage_overdue_days)

    level = _band(days_to_joining, days_since_contact, max_stage_overdue_days)

    # The blocker floor can only raise the band. Because the band is decided
    # here and the score is mapped into it afterwards, blocker *points* can
    # never move a candidate across a band boundary on their own — only the
    # floor does that, deliberately and visibly.
    floor_level = BLOCKER_FLOOR[blocker_signal]
    if _BAND_RANK[floor_level] > _BAND_RANK[level]:
        level = floor_level

    raw = min(
        100.0,
        _raw_score(days_to_joining, days_since_contact, max_stage_overdue_days)
        + BLOCKER_POINTS[blocker_signal],
    )

    # Map the 0-100 raw severity proportionally into the band's own window
    # rather than clamping to its edge. Clamping would collapse every
    # mildly-HIGH candidate onto exactly 70.0 and destroy the ranking the
    # score exists to provide.
    floor, ceiling = BAND_RANGES[level]
    return round(floor + (ceiling - floor) * (raw / 100.0), 1), level


def _band(
    days_to_joining: int, days_since_contact: int, max_stage_overdue_days: int
) -> RiskLevel:
    if (
        days_since_contact >= 10
        or (days_since_contact >= 5 and days_to_joining <= 14)
        or (max_stage_overdue_days > 7 and days_to_joining <= 21)
    ):
        return RiskLevel.HIGH

    if days_since_contact >= 7 or days_to_joining <= 7 or max_stage_overdue_days > 0:
        return RiskLevel.MEDIUM

    return RiskLevel.LOW


def _raw_score(
    days_to_joining: int, days_since_contact: int, max_stage_overdue_days: int
) -> float:
    silence = (
        min(days_since_contact, SILENCE_SATURATION_DAYS)
        / SILENCE_SATURATION_DAYS
        * SILENCE_MAX_POINTS
    )

    if days_to_joining <= 0:
        imminence = IMMINENCE_MAX_POINTS
    else:
        remaining = max(0, IMMINENCE_HORIZON_DAYS - days_to_joining)
        imminence = remaining / IMMINENCE_HORIZON_DAYS * IMMINENCE_MAX_POINTS

    lag = (
        min(max_stage_overdue_days, STAGE_LAG_SATURATION_DAYS)
        / STAGE_LAG_SATURATION_DAYS
        * STAGE_LAG_MAX_POINTS
    )

    return silence + imminence + lag


# ---------------------------------------------------------------------------
# Database-facing layer
# ---------------------------------------------------------------------------


def _days_since_contact(candidate: Candidate, today: date) -> int:
    """Never-contacted candidates count silence from offer_date — the clock
    starts when we made them an offer, not when we first bothered to call."""
    if candidate.last_interaction_at is None:
        return max(0, (today - candidate.offer_date).days)
    return max(0, (today - candidate.last_interaction_at.date()).days)


def resolve_blocker_signal(interactions: list[Interaction], today: date) -> BlockerSignal:
    """Reduce a candidate's interactions to their single most severe recent
    blocker signal.

    Only call notes carry these fields (CLAUDE.md scopes blocker_raised /
    blocker_category / recruiter_read to "the recruiter's structured read
    captured right after the call"), so a concern voiced in an email or
    WhatsApp message is invisible here by design — that is the content the
    Module 5 AI reads.

    Signals older than BLOCKER_SIGNAL_WINDOW_DAYS are ignored.
    """
    cutoff = today - timedelta(days=BLOCKER_SIGNAL_WINDOW_DAYS)
    strongest = BlockerSignal.NONE

    for interaction in interactions:
        if interaction.occurred_at.date() < cutoff:
            continue

        signal = BlockerSignal.NONE

        if interaction.blocker_raised:
            if interaction.blocker_category in CRITICAL_BLOCKER_CATEGORIES:
                signal = BlockerSignal.CRITICAL
            else:
                signal = BlockerSignal.CONCERN

        if interaction.recruiter_read == RecruiterRead.WORRIED:
            signal = _stronger(signal, BlockerSignal.CONCERN)
        elif interaction.recruiter_read == RecruiterRead.UNSURE:
            signal = _stronger(signal, BlockerSignal.UNSURE)

        strongest = _stronger(strongest, signal)

    return strongest


def _stronger(a: BlockerSignal, b: BlockerSignal) -> BlockerSignal:
    return a if BLOCKER_SIGNAL_RANK[a] >= BLOCKER_SIGNAL_RANK[b] else b


def recent_interactions_cutoff(today: date) -> datetime:
    """Coarse SQL prefilter bound. Midnight UTC of the cutoff date, so it can
    never exclude an interaction that resolve_blocker_signal would keep."""
    return datetime.combine(
        today - timedelta(days=BLOCKER_SIGNAL_WINDOW_DAYS), time.min, tzinfo=timezone.utc
    )


def _max_stage_overdue_days(stages: list[CandidateStage], today: date) -> int:
    """Only still-open stages count. A stage that was completed late is no
    longer a lag — it has been dealt with."""
    overdue = [
        (today - cs.due_date).days
        for cs in stages
        if cs.status in (StageStatus.PENDING, StageStatus.IN_PROGRESS) and cs.due_date < today
    ]
    return max(overdue, default=0)


@dataclass(frozen=True)
class RuleFloor:
    """The deterministic rule result for one candidate, with the inputs that
    produced it.

    The inputs travel with the result because two other places need them and
    should not each re-derive them: the Module 5 prompt builder, which puts
    "silent for 11 days" in front of the model as context, and the Module 5
    fallback, which has to explain a risk level with no LLM available.
    """

    score: float
    level: RiskLevel
    days_to_joining: int
    days_since_contact: int
    max_stage_overdue_days: int
    blocker_signal: BlockerSignal


def rule_floor(
    candidate: Candidate,
    stages: list[CandidateStage],
    interactions: list[Interaction],
    today: date,
) -> RuleFloor:
    """Score a candidate against the rules without touching the database or
    the candidate record. The single place the three rule inputs are derived
    from ORM objects."""
    days_to_joining = (candidate.joining_date - today).days
    days_since_contact = _days_since_contact(candidate, today)
    overdue = _max_stage_overdue_days(stages, today)
    blocker_signal = resolve_blocker_signal(interactions, today)

    score, level = compute_base_risk(
        days_to_joining=days_to_joining,
        days_since_contact=days_since_contact,
        max_stage_overdue_days=overdue,
        blocker_signal=blocker_signal,
    )
    return RuleFloor(
        score=score,
        level=level,
        days_to_joining=days_to_joining,
        days_since_contact=days_since_contact,
        max_stage_overdue_days=overdue,
        blocker_signal=blocker_signal,
    )


def _load_rule_inputs(
    db: Session, candidate: Candidate, today: date
) -> tuple[list[CandidateStage], list[Interaction]]:
    stages = list(
        db.scalars(select(CandidateStage).where(CandidateStage.candidate_id == candidate.id))
    )
    interactions = list(
        db.scalars(
            select(Interaction).where(
                Interaction.candidate_id == candidate.id,
                Interaction.occurred_at >= recent_interactions_cutoff(today),
            )
        )
    )
    return stages, interactions


def rule_floor_for_candidate(db: Session, candidate: Candidate, today: date) -> RuleFloor:
    """rule_floor(), loading the candidate's stages and recent interactions."""
    stages, interactions = _load_rule_inputs(db, candidate, today)
    return rule_floor(candidate, stages, interactions, today)


def is_higher(level: RiskLevel, than: RiskLevel) -> bool:
    """Band comparison, so callers outside this module never need _BAND_RANK.

    This is the whole of `final = max(base, ai_assessment)`: Module 5 asks
    whether the AI's level is higher than the rule floor and only then writes
    it. There is no path that lets an AI answer lower the level.
    """
    return _BAND_RANK[level] > _BAND_RANK[than]


def _apply(
    db: Session,
    candidate: Candidate,
    stages: list[CandidateStage],
    interactions: list[Interaction],
    today: date,
    actor: str,
    record_audit: bool = True,
) -> str:
    """Score one candidate inside the caller's transaction. Does not commit.

    Returns one of: "changed", "unchanged", "hr_override", "ai_higher".
    """
    floor = rule_floor(candidate, stages, interactions, today)
    score, level = floor.score, floor.level

    # The rule floor is always recorded, even when it does not win — the UI
    # shows it beside the final badge so an override is visibly an override.
    candidate.risk_score_base = score

    if candidate.risk_source == RiskSource.HR_OVERRIDE:
        return "hr_override"

    # CLAUDE.md: final = max(base, ai_assessment) — the AI may only raise.
    # Without this, a nightly recompute would silently erase a higher AI
    # assessment once Module 5 lands.
    if candidate.risk_source == RiskSource.AI and _BAND_RANK[candidate.risk_level] > _BAND_RANK[level]:
        return "ai_higher"

    if candidate.risk_level == level and candidate.risk_source == RiskSource.RULE:
        return "unchanged"

    previous_level = candidate.risk_level
    previous_source = candidate.risk_source
    candidate.risk_level = level
    candidate.risk_source = RiskSource.RULE

    if previous_level == level:
        # Only the source label moved (e.g. ai -> rule); not a risk change.
        return "unchanged"

    if record_audit:
        audit_service.record(
            db,
            entity_type="candidate",
            entity_id=candidate.id,
            action="risk_recompute",
            actor=actor,
            before={"risk_level": previous_level.value, "risk_source": previous_source.value},
            after={"risk_level": level.value, "risk_source": RiskSource.RULE.value},
        )
    return "changed"


def recompute_for_candidate(
    db: Session, candidate: Candidate, today: date, actor: str = "system"
) -> bool:
    """Rescore a single candidate in the caller's transaction, without
    committing — so it can join an interaction write and land atomically.

    Returns True if risk_level actually changed.
    """
    if candidate.final_outcome != FinalOutcome.PENDING:
        return False

    stages, interactions = _load_rule_inputs(db, candidate, today)
    return _apply(db, candidate, stages, interactions, today, actor) == "changed"


def recompute_all(
    db: Session, today: date, actor: str = "system", record_audit: bool = True
) -> dict:
    """Rescore every pending candidate and commit once.

    Candidates whose final_outcome is not PENDING are excluded entirely —
    they have already joined or dropped out, so "will they show up?" is no
    longer a question worth scoring.

    Deterministic for a given `today`, so running it twice in a row is a
    no-op: the second pass finds nothing changed and writes no audit rows.

    `record_audit=False` is for the seed, which is establishing initial
    state rather than transitioning anything — a fresh database should not
    open with an audit trail describing LOW -> HIGH moves that never
    happened to a real candidate.
    """
    candidates = list(
        db.scalars(select(Candidate).where(Candidate.final_outcome == FinalOutcome.PENDING))
    )

    stages_by_candidate: dict = defaultdict(list)
    interactions_by_candidate: dict = defaultdict(list)
    if candidates:
        candidate_ids = [c.id for c in candidates]
        for cs in db.scalars(
            select(CandidateStage).where(CandidateStage.candidate_id.in_(candidate_ids))
        ):
            stages_by_candidate[cs.candidate_id].append(cs)

        # Only recent interactions can carry a live blocker signal, so the
        # sweep never loads the full history.
        for interaction in db.scalars(
            select(Interaction).where(
                Interaction.candidate_id.in_(candidate_ids),
                Interaction.occurred_at >= recent_interactions_cutoff(today),
            )
        ):
            interactions_by_candidate[interaction.candidate_id].append(interaction)

    outcomes = {"changed": 0, "unchanged": 0, "hr_override": 0, "ai_higher": 0}
    for candidate in candidates:
        result = _apply(
            db,
            candidate,
            stages_by_candidate[candidate.id],
            interactions_by_candidate[candidate.id],
            today,
            actor,
            record_audit,
        )
        outcomes[result] += 1

    db.commit()

    distribution = {level.value: 0 for level in RiskLevel}
    for candidate in candidates:
        distribution[candidate.risk_level.value] += 1

    return {
        "scanned": len(candidates),
        "score_updated": len(candidates),
        "level_changed": outcomes["changed"],
        "skipped_hr_override": outcomes["hr_override"],
        "skipped_ai_higher": outcomes["ai_higher"],
        "distribution": distribution,
    }
