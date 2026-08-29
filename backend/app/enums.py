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
