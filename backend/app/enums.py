"""Every enum referenced by the Module 1 schema in CLAUDE.md, as Python str Enums."""

from enum import Enum


class EngagementStatus(str, Enum):
    OFFER_ACCEPTED = "offer_accepted"
    WELCOME_SENT = "welcome_sent"
    DOCUMENTATION = "documentation"
    MANAGER_INTRO = "manager_intro"
    TEAM_CONTEXT = "team_context"
    RELOCATION_CHECK = "relocation_check"
    PRE_JOINING_CHECKIN = "pre_joining_checkin"
    JOINED = "joined"
    DROPPED_OUT = "dropped_out"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskSource(str, Enum):
    RULE = "rule"
    AI = "ai"
    HR_OVERRIDE = "hr_override"


class FinalOutcome(str, Enum):
    PENDING = "pending"
    JOINED = "joined"
    DROPPED_OUT = "dropped_out"


class StageAnchor(str, Enum):
    OFFER = "offer"
    JOINING = "joining"


class StageStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class InteractionChannel(str, Enum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    CALL = "call"
    IN_PERSON = "in_person"


class InteractionDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class RecruiterRead(str, Enum):
    ON_TRACK = "on_track"
    UNSURE = "unsure"
    WORRIED = "worried"


class BlockerCategory(str, Enum):
    RELOCATION = "relocation"
    NOTICE_PERIOD = "notice_period"
    COUNTER_OFFER = "counter_offer"
    COMPENSATION = "compensation"
    ROLE_SCOPE = "role_scope"
    PERSONAL = "personal"
    NONE = "none"


class BlockerSignal(str, Enum):
    """Severity of the structured concern a recruiter captured on a call note.

    Derived from interactions.blocker_raised / blocker_category /
    recruiter_read and fed into the risk rule floor. Ordered least to most
    severe; see risk_service.BLOCKER_SIGNAL_RANK.

    This is deterministic and human-tagged — the recruiter already told us
    what the blocker was. It needs no AI to read.
    """

    NONE = "none"
    UNSURE = "unsure"  # recruiter_read='unsure' — nudge only, never a band change
    CONCERN = "concern"  # a blocker was raised, or recruiter_read='worried'
    CRITICAL = "critical"  # counter_offer or notice_period blocker


class CandidateSort(str, Enum):
    """Sort orders GET /candidates accepts.

    RISK is what turns the list into the triage tool the product is for —
    "the five worth a phone call this morning" — ordering by final risk band
    and then by risk_score_base within it. JOINING_DATE stays the default so
    the plain list keeps reading as a calendar.
    """

    JOINING_DATE = "joining_date"
    RISK = "risk"


class ValidationStatus(str, Enum):
    VALID = "valid"
    REPAIRED = "repaired"
    FAILED = "failed"


class FollowUpPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class FollowUpStatus(str, Enum):
    OPEN = "open"
    DONE = "done"
    DISMISSED = "dismissed"


class FollowUpSource(str, Enum):
    AUTOMATION = "automation"
    AI = "ai"
    MANUAL = "manual"


# journey_stages.key -> candidates.engagement_status.
#
# Single source of truth, shared by seed.py and stage_service so completing a
# stage through the API lands a candidate on the same engagement_status the
# seed would have given them.
#
# "joining" is deliberately absent: a candidate becomes JOINED because
# final_outcome says so, not merely because the joining stage's due date
# arrived and someone ticked it off.
STAGE_KEY_TO_ENGAGEMENT_STATUS: dict[str, EngagementStatus] = {
    "offer_accepted": EngagementStatus.OFFER_ACCEPTED,
    "welcome": EngagementStatus.WELCOME_SENT,
    "documentation": EngagementStatus.DOCUMENTATION,
    "manager_intro": EngagementStatus.MANAGER_INTRO,
    "team_context": EngagementStatus.TEAM_CONTEXT,
    "relocation_check": EngagementStatus.RELOCATION_CHECK,
    "pre_joining_checkin": EngagementStatus.PRE_JOINING_CHECKIN,
}
