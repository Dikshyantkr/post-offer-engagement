"""Idempotent demo data: recruiters, the journey_stages template, 50+ candidates
with realistic interaction histories, and materialised candidate_stages rows.

Safe to run twice — candidates, recruiters and journey_stages are all looked up
by a natural key (email / key) before insert, so re-running never duplicates.
Uses the real current date as its anchor (this is a data-generation script, not
the pure stage_scheduler function, so datetime.now()-style anchoring is fine
and expected here).
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Base, SessionLocal, engine
from app.enums import (
    BlockerCategory,
    EngagementStatus,
    FinalOutcome,
    InteractionChannel,
    InteractionDirection,
    RecruiterRead,
    RiskSource,
    StageAnchor,
    StageStatus,
)
from app.models import Candidate, CandidateStage, Interaction, JourneyStage, Recruiter
from app.services import risk_service
from app.services.stage_scheduler import compute_stage_schedule
from app.services.stage_service import resolve_engagement_status

random.seed(20260829)

NOW: date = date.today()

# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

RECRUITERS: list[tuple[str, str]] = [
    ("Ananya Rao", "ananya.rao@company.com"),
    ("Devraj Singh", "devraj.singh@company.com"),
    ("Meera Iyer", "meera.iyer@company.com"),
    ("Farhan Sheikh", "farhan.sheikh@company.com"),
    ("Priya Nair", "priya.nair@company.com"),
    ("Karan Mehta", "karan.mehta@company.com"),
]

ROLE_DEPARTMENTS: dict[str, str] = {
    "Software Engineer II": "Engineering",
    "Senior Backend Engineer": "Engineering",
    "DevOps Engineer": "Platform",
    "QA Engineer": "Engineering",
    "Engineering Manager": "Engineering",
    "Product Manager": "Product",
    "UX Designer": "Design",
    "Data Analyst": "Data",
    "Data Scientist": "Data",
}
ROLES = list(ROLE_DEPARTMENTS)

LOCATIONS = ["Bengaluru", "Pune", "Hyderabad", "Gurugram", "Remote"]

JOURNEY_STAGE_DEFS: list[dict] = [
    dict(key="offer_accepted", label="Offer accepted", anchor=StageAnchor.OFFER, offset_days=0, sequence_order=1),
    dict(key="welcome", label="Welcome", anchor=StageAnchor.OFFER, offset_days=1, sequence_order=2),
    dict(key="documentation", label="Documentation", anchor=StageAnchor.OFFER, offset_days=3, sequence_order=3),
    dict(key="manager_intro", label="Manager introduction", anchor=StageAnchor.OFFER, offset_days=21, sequence_order=4),
    dict(key="team_context", label="Team & role context", anchor=StageAnchor.OFFER, offset_days=35, sequence_order=5),
    dict(key="relocation_check", label="Relocation & logistics check", anchor=StageAnchor.JOINING, offset_days=-25, sequence_order=6),
    dict(key="pre_joining_checkin", label="Pre-joining check-in", anchor=StageAnchor.JOINING, offset_days=-10, sequence_order=7),
    dict(key="joining", label="Joining", anchor=StageAnchor.JOINING, offset_days=0, sequence_order=8),
]

FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditi", "Diya", "Kabir", "Ishaan", "Ananya", "Riya", "Yash", "Tara",
    "Nikhil", "Sneha", "Rahul", "Pooja", "Varun", "Meghna", "Sameer", "Anjali", "Rohit", "Divya",
    "Manish", "Shreya", "Arnav", "Kritika", "Siddharth", "Nandini", "Gaurav", "Simran", "Ajay", "Preeti",
    "Harsh", "Radhika", "Deepak", "Swati", "Vikas", "Neelam", "Suresh", "Anita", "Rajesh", "Kavita",
    "Amit", "Ritu", "Sanjay", "Pallavi", "Vinay", "Alka", "Naveen", "Bhavna", "Tarun", "Isha",
]
LAST_NAMES = [
    "Sharma", "Verma", "Gupta", "Iyer", "Nair", "Reddy", "Kumar", "Singh", "Patel", "Menon",
    "Bose", "Chatterjee", "Rao", "Pillai", "Joshi", "Bhatt", "Sarin", "Dutta", "Saxena", "Agarwal",
    "Shetty", "Pandey", "Mishra", "Bansal", "Chawla",
]

OUTBOUND_TEMPLATES = [
    "Hi {first}, just checking in ahead of your start date. Let us know if you need anything from our end.",
    "Hello {first}, sharing the onboarding documents for the {role} role — please review and confirm once done.",
    "Hi {first}, wanted to loop in your manager for an intro call. Sending a calendar invite shortly.",
    "Hi {first}, hope the notice period is going smoothly. Any updates from your side on the move to {location}?",
    "Hello {first}, a quick reminder about the pre-joining checklist before your start date.",
    "Hi {first}, sharing some context on the team and what the first few weeks will look like.",
]
INBOUND_TEMPLATES = [
    "Hi, thanks for checking in. Everything's on track from my side, will confirm once I have updates.",
    "Hello, received the documents, will send them back signed by this week.",
    "Thanks for the intro, looking forward to the call with the team.",
    "Hi, yes still on track. Will let you know if anything changes.",
    "Got it, thank you for the update.",
    "Sounds good, appreciate you keeping me posted.",
]

# ---------------------------------------------------------------------------
# Hand-written high-risk candidates
# ---------------------------------------------------------------------------
# Each has HAND-WRITTEN inbound messages meant to read like a real, hesitant
# person, not an LLM describing concern. `days_to_joining` is always <=
# `notice_days` so offer_date never lands in the future relative to NOW.

HIGH_RISK_CANDIDATES = [
    {
        "name": "Ritika Bansal",
        "role": "Product Manager",
        "location": "Pune",
        "recruiter_email": "priya.nair@company.com",
        "notice_days": 60,
        "days_to_joining": 9,
        "notes": "Relocation to Pune still unresolved this close to joining. Follow up on housing.",
        "interactions": [
            ("outbound", "whatsapp", -18, "Hi Ritika, just checking in on how the move to Pune is coming along. Anything we can help with?"),
            ("inbound", "whatsapp", -6,
             "hi, sorry for the delay in replying. still trying to sort out a place to stay in Pune, "
             "landlords keep asking for documents I don't have yet since I'm not local. might need a few "
             "more days to figure this out, will update you"),
            ("outbound", "email", -3, "Hi Ritika, totally understand — let us know if a temporary corporate stay would help bridge the gap while you sort out a place."),
        ],
        "blocker_category": BlockerCategory.RELOCATION,
    },
    {
        "name": "Aditya Kulkarni",
        "role": "Senior Backend Engineer",
        "location": "Hyderabad",
        "recruiter_email": "devraj.singh@company.com",
        "notice_days": 90,
        "days_to_joining": 25,
        "notes": "Current employer disputing relieving date. Watch this one — 90-day notice buys some slack but escalate if unresolved past next check-in.",
        "interactions": [
            ("outbound", "email", -20, "Hi Aditya, hope the transition is going well. How's the handover on your current project shaping up?"),
            ("inbound", "email", -5,
             "Hi, wanted to give you an update — my current manager is not agreeing to relieve me before "
             "the 90 days, he says there's no one to hand over my project to right now. I've asked HR here "
             "to see if it can be reduced but no confirmation yet. Will keep you posted once I know more."),
            ("outbound", "call", -2, "Called to understand the notice period situation in more detail."),
        ],
        "blocker_category": BlockerCategory.NOTICE_PERIOD,
        "call_recruiter_read": RecruiterRead.UNSURE,
        "call_date_confirmed": False,
    },
    {
        "name": "Sana Qureshi",
        "role": "Data Scientist",
        "location": "Bengaluru",
        "recruiter_email": "meera.iyer@company.com",
        "notice_days": 30,
        "days_to_joining": 6,
        "notes": "Mentioned current company called her in for a conversation right after resigning. Classic counter-offer pattern — prioritise this call.",
        "interactions": [
            ("outbound", "whatsapp", -10, "Hi Sana, excited to have you join the data team soon! Let us know if you need anything before your start date."),
            ("inbound", "whatsapp", -4,
             "hey, quick one — my current company found out I'm leaving and they've called me in for a "
             "chat tomorrow. not sure what it's about tbh but just letting you know in case there's any "
             "delay from my side"),
            ("outbound", "whatsapp", -3, "Thanks for the heads up, Sana. Let us know how the conversation goes — happy to jump on a call if useful."),
        ],
        "blocker_category": BlockerCategory.COUNTER_OFFER,
    },
    {
        "name": "Vikram Chatterjee",
        "role": "Engineering Manager",
        "location": "Gurugram",
        "recruiter_email": "karan.mehta@company.com",
        "notice_days": 60,
        "days_to_joining": 14,
        "notes": "Hedging on compensation — 'should be fine' language while waiting on his current company's counter.",
        "interactions": [
            ("outbound", "email", -21, "Hi Vikram, looking forward to having you lead the platform team. How's the notice period going?"),
            ("inbound", "email", -7,
             "hi, all good on my end, just waiting to hear back on the final numbers from my current company "
             "before I close things out here. should be fine either way, will confirm once I know"),
        ],
        "blocker_category": BlockerCategory.COMPENSATION,
    },
    {
        "name": "Neha Deshmukh",
        "role": "UX Designer",
        "location": "Remote",
        "recruiter_email": "farhan.sheikh@company.com",
        "notice_days": 90,
        "days_to_joining": 40,
        "notes": "Vague hedging about role scope after the team intro call — 'probably just how it was explained'. Worth a clarifying conversation.",
        "interactions": [
            ("outbound", "email", -30, "Hi Neha, setting up an intro call with the design team so you get a feel for the day-to-day work."),
            ("outbound", "call", -15, "Intro call with design team completed."),
            ("inbound", "email", -9,
             "Hi, thanks for the intro call with the team. Just to double check — the day to day sounds a "
             "bit different from what came up during the interviews, but I think it's probably just how it "
             "was explained. Should be fine, as of now I don't have major concerns."),
        ],
        "blocker_category": BlockerCategory.ROLE_SCOPE,
        # The logged call happened before the hedge surfaced in her follow-up
        # email, so it carries no blocker of its own — it was a routine, on-track
        # check-in. Only email/whatsapp content can carry the actual concern here,
        # since blocker_category is a call-note-only field.
        "call_blocker_category": BlockerCategory.NONE,
        "call_recruiter_read": RecruiterRead.ON_TRACK,
        "call_date_confirmed": True,
    },
    {
        "name": "Arjun Malhotra",
        "role": "DevOps Engineer",
        "location": "Bengaluru",
        "recruiter_email": "ananya.rao@company.com",
        "notice_days": 30,
        "days_to_joining": 5,
        "notes": "Gone completely silent with joining days away. No replies to last three outreach attempts.",
        "interactions": [
            ("outbound", "email", -22, "Hi Arjun, welcome aboard! Sharing the onboarding documents ahead of your start date."),
            ("outbound", "whatsapp", -17, "Hi Arjun, just checking you received the documents okay. Let us know if anything's unclear."),
            ("outbound", "call", -12, "Called twice, no answer either time. Left a voicemail asking Arjun to call back."),
        ],
        "blocker_category": BlockerCategory.NONE,
        "call_recruiter_read": RecruiterRead.WORRIED,
        "call_date_confirmed": None,
    },
    {
        "name": "Kavya Subramaniam",
        "role": "Software Engineer II",
        "location": "Hyderabad",
        "recruiter_email": "meera.iyer@company.com",
        "notice_days": 60,
        "days_to_joining": 20,
        "notes": "Went dark right after the welcome email. No response since — flagging for a personal call, not another message.",
        "interactions": [
            ("outbound", "email", -30, "Hi Kavya, welcome to the team! Sharing a few documents to get started with onboarding."),
            ("outbound", "whatsapp", -15, "Hi Kavya, following up on the onboarding documents whenever you get a chance."),
        ],
        "blocker_category": BlockerCategory.NONE,
    },
    {
        "name": "Rohan Kapoor",
        "role": "QA Engineer",
        "location": "Pune",
        "recruiter_email": "priya.nair@company.com",
        "notice_days": 90,
        "days_to_joining": 18,
        "notes": "Family emergency raised directly by the candidate. Give him space but keep a light touch check-in scheduled.",
        "interactions": [
            ("inbound", "whatsapp", -8,
             "hi, really sorry, my father was hospitalized last week so things have been chaotic. I do want "
             "to join, just need some time before I can think clearly about logistics. will reach out once "
             "things settle a bit"),
            ("outbound", "whatsapp", -7, "Rohan, so sorry to hear that — please take the time you need, we'll check in again in a couple of weeks unless you reach out sooner."),
            ("outbound", "call", -2, "Light-touch check-in call as agreed — kept it brief, didn't push on logistics yet."),
        ],
        "blocker_category": BlockerCategory.PERSONAL,
        "call_recruiter_read": RecruiterRead.WORRIED,
        "call_date_confirmed": None,
    },
    {
        "name": "Ishita Ghosh",
        "role": "Data Analyst",
        "location": "Gurugram",
        "recruiter_email": "farhan.sheikh@company.com",
        "notice_days": 30,
        "days_to_joining": 10,
        "notes": "Directly questioning the offer number against her current CTC. Needs a compensation conversation, not a generic nudge.",
        "interactions": [
            ("outbound", "email", -12, "Hi Ishita, looking forward to having you join the data team. Let us know if you have any questions before your start date."),
            ("inbound", "email", -5,
             "Hi, I wanted to be upfront — the offer is a bit lower than what I was expecting based on my "
             "current CTC, and it's making it harder to justify the move to my family. Is there any room to "
             "relook at the number, or should I understand this is final?"),
        ],
        "blocker_category": BlockerCategory.COMPENSATION,
    },
    {
        "name": "Aarav Sharma",
        "role": "Software Engineer II",
        "location": "Bengaluru",
        "recruiter_email": "devraj.singh@company.com",
        "notice_days": 60,
        "days_to_joining": 12,
        "notes": "Hedging around the money math out loud — hasn't said no, but hasn't committed either. Keep an eye before the notice window closes.",
        "interactions": [
            ("outbound", "whatsapp", -25, "hi aarav, congrats again on the offer! sharing the onboarding checklist below, let us know if anything's unclear."),
            ("inbound", "whatsapp", -8,
             "hii sorry been a bit slow this week, just going back and forth doing some math on the move — "
             "rent + everything here vs there. nothing decided just yet, will loop back once i've actually "
             "sat down and worked through it properly"),
            ("outbound", "whatsapp", -3, "no worries aarav, take your time — happy to hop on a call if it helps to talk through anything on our end."),
        ],
        "blocker_category": BlockerCategory.COMPENSATION,
    },
    {
        "name": "Arnav Mishra",
        "role": "Product Manager",
        "location": "Gurugram",
        "recruiter_email": "ananya.rao@company.com",
        "notice_days": 90,
        "days_to_joining": 30,
        "notes": "Quietly unsure about what the role actually involves day-to-day. Worth a clarifying call before he second-guesses further.",
        "interactions": [
            ("outbound", "email", -20, "hi arnav, sharing the role brief and some docs from the team ahead of your start date."),
            ("inbound", "email", -6,
             "hey, went through everything, mostly fine. just a bit confused on what the day to day actually "
             "looks like, some of it reads a little different from what came up in the interviews but could "
             "just be me reading too much into it. not urgent, just flagging"),
        ],
        "blocker_category": BlockerCategory.ROLE_SCOPE,
    },
    {
        "name": "Ishaan Reddy",
        "role": "Data Analyst",
        "location": "Pune",
        "recruiter_email": "karan.mehta@company.com",
        "notice_days": 30,
        "days_to_joining": 7,
        "notes": "Family isn't fully on board with the move. Give him room but don't let this go unchecked.",
        "interactions": [
            ("inbound", "whatsapp", -5,
             "hey sorry for going a bit quiet, some stuff going on at home right now, parents aren't fully on "
             "board with me moving cities at the moment. still working through it on my end, nothing major "
             "just need a bit of time"),
            ("outbound", "whatsapp", -4, "totally understand ishaan, no pressure at all — just let us know whenever you're ready to talk, we're not going anywhere."),
        ],
        "blocker_category": BlockerCategory.PERSONAL,
    },
    {
        "name": "Kavita Joshi",
        "role": "Engineering Manager",
        "location": "Hyderabad",
        "recruiter_email": "meera.iyer@company.com",
        "notice_days": 60,
        "days_to_joining": 16,
        "notes": "Text replies read fine on the surface — the real hesitation only came out on a call. Trust the call note over the chat history here.",
        "interactions": [
            ("outbound", "whatsapp", -30, "hi kavita, welcome aboard! sharing a few documents to kick off the onboarding process."),
            ("inbound", "whatsapp", -21, "yeah should be fine, will keep you posted"),
            ("outbound", "call", -4,
             "Called since she'd gone quiet for a couple weeks. On the phone she mentioned she's 'still "
             "weighing a couple of things' before fully committing — wouldn't say more than that, but didn't "
             "sound settled either."),
        ],
        "blocker_category": BlockerCategory.COMPENSATION,
        "call_recruiter_read": RecruiterRead.UNSURE,
        "call_date_confirmed": False,
    },
    {
        "name": "Neelam Bose",
        "role": "UX Designer",
        "location": "Remote",
        "recruiter_email": "priya.nair@company.com",
        "notice_days": 90,
        "days_to_joining": 22,
        "notes": "Was engaged early, went quiet after the manager intro. Call didn't get much more out of her either — flag for a second, more direct conversation.",
        "interactions": [
            ("outbound", "email", -40, "hi neelam, excited to have you on the team! sharing the welcome documents to get things started."),
            ("inbound", "email", -38, "thank you so much! this all looks great, will get the signed copies back to you by tomorrow"),
            ("outbound", "email", -14, "hi neelam, just checking in ahead of the manager intro call next week — let us know if the timing works."),
            ("outbound", "call", -3,
             "Reached her by phone after two weeks of silence. She was polite but distracted, said she's "
             "'still figuring a few things out' before confirming anything — didn't want to get into "
             "specifics on the call."),
        ],
        "blocker_category": BlockerCategory.NONE,
        "call_recruiter_read": RecruiterRead.WORRIED,
        "call_date_confirmed": False,
    },
    {
        "name": "Yash Patel",
        "role": "DevOps Engineer",
        "location": "Bengaluru",
        "recruiter_email": "farhan.sheikh@company.com",
        "notice_days": 30,
        "days_to_joining": 9,
        "notes": "Replies have been getting shorter each time we check in. Nothing said outright, but the trend itself is the signal.",
        "interactions": [
            ("outbound", "whatsapp", -19, "hi yash, congrats again on the offer! sharing the onboarding checklist, let us know if you have questions."),
            ("inbound", "whatsapp", -17, "thanks so much! will go through everything this week, looks pretty straightforward so far"),
            ("outbound", "whatsapp", -10, "hey yash, just checking — how did the manager intro call go on your end?"),
            ("inbound", "whatsapp", -9, "yeah it was fine"),
            ("outbound", "whatsapp", -3, "hi yash, excited to have you joining soon! anything you need from us before the start date?"),
            ("inbound", "whatsapp", -2, "nope all good"),
        ],
        "blocker_category": BlockerCategory.NONE,
    },
]

# Names above generated by the routine index-based generator, before they were
# hand-written with real narratives. Skipped in _generate_routine_candidate so
# they aren't created a second time under a different email.
ROUTINE_INDEX_SKIP = {0, 5, 8, 22, 35, 39}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dt(d: date, hour: int = 10) -> datetime:
    return datetime.combine(d, time(hour=hour, minute=0)).replace(tzinfo=timezone.utc)


def _pick_notice_days() -> int:
    return random.choices([30, 60, 90], weights=[0.35, 0.35, 0.30])[0]


def seed_recruiters(db: Session) -> dict[str, Recruiter]:
    existing = {r.email: r for r in db.scalars(select(Recruiter)).all()}
    for name, email in RECRUITERS:
        if email not in existing:
            recruiter = Recruiter(name=name, email=email)
            db.add(recruiter)
            existing[email] = recruiter
    db.flush()
    return existing


def seed_journey_stages(db: Session) -> list[JourneyStage]:
    existing = {s.key: s for s in db.scalars(select(JourneyStage)).all()}
    for stage_def in JOURNEY_STAGE_DEFS:
        stage = existing.get(stage_def["key"])
        if stage is None:
            stage = JourneyStage(**stage_def)
            db.add(stage)
            existing[stage_def["key"]] = stage
        else:
            stage.label = stage_def["label"]
            stage.anchor = stage_def["anchor"]
            stage.offset_days = stage_def["offset_days"]
            stage.sequence_order = stage_def["sequence_order"]
            stage.is_active = True
    db.flush()
    return sorted(existing.values(), key=lambda s: s.sequence_order)


def _materialise_stages(
    candidate: Candidate,
    journey_stages: list[JourneyStage],
    outcome: FinalOutcome,
    recruiter_name: str,
) -> None:
    schedule = compute_stage_schedule(candidate.offer_date, candidate.joining_date, journey_stages)
    stage_by_key = {s.key: s for s in journey_stages}

    dropout_cutoff = random.randint(1, len(schedule) - 1) if outcome == FinalOutcome.DROPPED_OUT else None

    for index, (key, due_date) in enumerate(schedule):
        if outcome == FinalOutcome.JOINED:
            status = StageStatus.COMPLETED
        elif outcome == FinalOutcome.DROPPED_OUT:
            if index < dropout_cutoff:
                status = StageStatus.COMPLETED
            elif index == dropout_cutoff:
                status = StageStatus.PENDING
            else:
                status = StageStatus.SKIPPED
        else:
            status = StageStatus.COMPLETED if due_date <= NOW else StageStatus.PENDING

        candidate.stages.append(
            CandidateStage(
                stage=stage_by_key[key],
                due_date=due_date,
                status=status,
                completed_at=_dt(due_date) if status == StageStatus.COMPLETED else None,
                completed_by=recruiter_name if status == StageStatus.COMPLETED else None,
            )
        )


def _resolve_pending_engagement_status(candidate: Candidate) -> EngagementStatus:
    # Delegates to the same resolver the API uses, so a seeded candidate and
    # one advanced through POST /stages/{id}/complete agree on the rules.
    return resolve_engagement_status(candidate.stages)


def _add_interaction(
    candidate: Candidate,
    recruiter: Recruiter,
    direction: InteractionDirection,
    channel: InteractionChannel,
    occurred_at: datetime,
    content: str,
    blocker_raised: bool = False,
    blocker_category: BlockerCategory | None = None,
    recruiter_read: RecruiterRead | None = None,
    date_confirmed: bool | None = None,
) -> Interaction:
    interaction = Interaction(
        candidate=candidate,
        channel=channel,
        direction=direction,
        content=content,
        occurred_at=occurred_at,
        created_by=recruiter.name,
        blocker_raised=blocker_raised,
        blocker_category=blocker_category,
        date_confirmed=date_confirmed,
        recruiter_read=recruiter_read,
    )
    return interaction


def _create_high_risk_candidate(
    db: Session,
    spec: dict,
    recruiters_by_email: dict[str, Recruiter],
    journey_stages: list[JourneyStage],
) -> None:
    email = f"{spec['name'].lower().replace(' ', '.')}@example.com"
    if db.scalar(select(Candidate).where(Candidate.email == email)) is not None:
        return

    recruiter = recruiters_by_email[spec["recruiter_email"]]
    joining_date = NOW + timedelta(days=spec["days_to_joining"])
    offer_date = joining_date - timedelta(days=spec["notice_days"])

    candidate = Candidate(
        name=spec["name"],
        email=email,
        phone=f"+91-9{random.randint(100000000, 999999999)}",
        role=spec["role"],
        department=ROLE_DEPARTMENTS[spec["role"]],
        location=spec["location"],
        offer_date=offer_date,
        joining_date=joining_date,
        recruiter=recruiter,
        final_outcome=FinalOutcome.PENDING,
        risk_source=RiskSource.RULE,
        notes=spec["notes"],
    )
    db.add(candidate)

    _materialise_stages(candidate, journey_stages, FinalOutcome.PENDING, recruiter.name)
    candidate.engagement_status = _resolve_pending_engagement_status(candidate)

    # CLAUDE.md: blocker_raised/blocker_category/date_confirmed/recruiter_read are
    # call-note fields only — "the recruiter's structured read captured at the
    # only moment it exists, right after the call". Inbound email/whatsapp text
    # carries the signal in its content for the AI to read verbatim, not as a
    # pre-tagged field.
    last_occurred: datetime | None = None
    for direction_str, channel_str, day_offset, content in spec["interactions"]:
        occurred_at = _dt(NOW + timedelta(days=day_offset))
        direction = InteractionDirection.INBOUND if direction_str == "inbound" else InteractionDirection.OUTBOUND
        channel = InteractionChannel(channel_str)
        is_call = channel == InteractionChannel.CALL
        call_blocker_category = spec.get("call_blocker_category", spec["blocker_category"])
        db.add(
            _add_interaction(
                candidate,
                recruiter,
                direction,
                channel,
                occurred_at,
                content,
                blocker_raised=is_call and call_blocker_category != BlockerCategory.NONE,
                blocker_category=call_blocker_category if is_call else None,
                recruiter_read=spec.get("call_recruiter_read") if is_call else None,
                date_confirmed=spec.get("call_date_confirmed") if is_call else None,
            )
        )
        if last_occurred is None or occurred_at > last_occurred:
            last_occurred = occurred_at

    candidate.last_interaction_at = last_occurred


def _generate_routine_candidate(
    db: Session,
    index: int,
    recruiters: list[Recruiter],
    journey_stages: list[JourneyStage],
) -> None:
    first = FIRST_NAMES[index % len(FIRST_NAMES)]
    last = LAST_NAMES[index % len(LAST_NAMES)]
    name = f"{first} {last}"
    email = f"{first.lower()}.{last.lower()}{index}@example.com"

    if db.scalar(select(Candidate).where(Candidate.email == email)) is not None:
        return

    role = random.choice(ROLES)
    location = random.choice(LOCATIONS)
    recruiter = random.choice(recruiters)
    notice_days = _pick_notice_days()

    is_past_cohort = random.random() < 0.35
    if is_past_cohort:
        days_to_joining = -random.randint(5, 90)
        outcome = FinalOutcome.DROPPED_OUT if random.random() < 0.2 else FinalOutcome.JOINED
    else:
        days_to_joining = random.randint(1, min(60, notice_days))
        outcome = FinalOutcome.DROPPED_OUT if random.random() < 0.08 else FinalOutcome.PENDING

    joining_date = NOW + timedelta(days=days_to_joining)
    offer_date = joining_date - timedelta(days=notice_days)

    candidate = Candidate(
        name=name,
        email=email,
        phone=f"+91-9{random.randint(100000000, 999999999)}",
        role=role,
        department=ROLE_DEPARTMENTS[role],
        location=location,
        offer_date=offer_date,
        joining_date=joining_date,
        recruiter=recruiter,
        final_outcome=outcome,
        risk_source=RiskSource.RULE,
    )
    db.add(candidate)

    _materialise_stages(candidate, journey_stages, outcome, recruiter.name)

    if outcome == FinalOutcome.JOINED:
        candidate.engagement_status = EngagementStatus.JOINED
    elif outcome == FinalOutcome.DROPPED_OUT:
        candidate.engagement_status = EngagementStatus.DROPPED_OUT
    else:
        candidate.engagement_status = _resolve_pending_engagement_status(candidate)

    interaction_count = random.randint(2, 6)
    upper_bound = min(NOW, joining_date)
    lower_bound = offer_date
    span_days = max((upper_bound - lower_bound).days, 0)

    # Sample distinct (day, hour) slots so two interactions can never land on
    # the exact same occurred_at — that's what previously produced literal
    # duplicate interaction rows when span_days was small relative to the
    # candidate's interaction count.
    hours = range(9, 18)
    all_slots = [(day, hour) for day in range(span_days + 1) for hour in hours]
    slots = sorted(random.sample(all_slots, k=min(interaction_count, len(all_slots))))

    last_occurred: datetime | None = None
    for i, (offset, hour) in enumerate(slots):
        occurred_day = lower_bound + timedelta(days=offset)
        occurred_at = _dt(occurred_day, hour=hour)
        direction = InteractionDirection.OUTBOUND if i % 2 == 0 else InteractionDirection.INBOUND
        channel = random.choice(list(InteractionChannel))
        template = random.choice(OUTBOUND_TEMPLATES if direction == InteractionDirection.OUTBOUND else INBOUND_TEMPLATES)
        content = template.format(first=first, role=role, location=location)
        db.add(_add_interaction(candidate, recruiter, direction, channel, occurred_at, content))
        if last_occurred is None or occurred_at > last_occurred:
            last_occurred = occurred_at

    candidate.last_interaction_at = last_occurred
    # risk_level / risk_score_base are left at their column defaults here and
    # filled in by the single risk_service.recompute_all() pass in main().
    # Resolved candidates (joined / dropped_out) are excluded from scoring and
    # keep those defaults, which is correct: "will they show up?" is settled.


def main() -> None:
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        recruiters_by_email = seed_recruiters(db)
        journey_stages = seed_journey_stages(db)
        db.commit()

        recruiters = list(recruiters_by_email.values())

        high_risk_count = 0
        for spec in HIGH_RISK_CANDIDATES:
            _create_high_risk_candidate(db, spec, recruiters_by_email, journey_stages)
            high_risk_count += 1
        db.commit()

        routine_count = 0
        target_routine = 45  # 15 hand-written + 39 generated (6 indices reassigned to hand-written) = 54 total
        for i in range(target_routine):
            if i in ROUTINE_INDEX_SKIP:
                continue
            _generate_routine_candidate(db, i, recruiters, journey_stages)
            routine_count += 1
        db.commit()

        # One definition of risk in the codebase: the seed scores its
        # candidates through the same rule engine the API and the nightly
        # sweep use, rather than a second heuristic that drifts from it.
        risk = risk_service.recompute_all(db, today=NOW, actor="seed", record_audit=False)

        count = len(db.scalars(select(Candidate)).all())
        print(f"Seed complete. {count} candidates in database "
              f"({high_risk_count} hand-written high-risk specs, {routine_count} generated).")
        print(f"Risk scored via risk_service: {risk['scanned']} pending candidates, "
              f"distribution {risk['distribution']}.")


if __name__ == "__main__":
    main()
