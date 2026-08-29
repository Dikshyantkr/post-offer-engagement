"""Versioned prompt templates and the candidate context builder.

Every prompt in this file is built from three parts: a role statement, the
shared evidence-weighting and guardrail blocks, and a rendered candidate
context. PROMPT_VERSION is stamped onto every ai_analyses row, so when a
prompt changes it is possible to tell which rows came from which wording —
without that, comparing yesterday's outputs to today's is guesswork.

The evidence-weighting block is the substantive idea here. The interaction
log mixes two very different kinds of text:

  * inbound email and WhatsApp — the candidate's own words, written by them,
    unedited. "not sure what it's about tbh" is data.
  * call notes — a recruiter's paraphrase, typed in a hurry after a call,
    already filtered through what they wanted to hear. "Spoke to him, seems
    fine" can sit on top of a candidate who is halfway out the door.

A model given both without instruction treats them as equally authoritative
and will happily let a cheerful call note outweigh a worried WhatsApp
message. Every prompt says explicitly which is which, and the rendered
context tags each interaction inline so the distinction survives into the
part of the prompt the model actually reads closely.
"""

from __future__ import annotations

from datetime import date

from app.enums import InteractionChannel, InteractionDirection, StageStatus
from app.models import Candidate, CandidateStage, Interaction
from app.services.risk_service import RuleFloor

PROMPT_VERSION = "v1"

# Enough to see a trajectory without burning context on the routine
# "thanks, noted" exchanges that dominate an older history.
MAX_INTERACTIONS_IN_PROMPT = 12

# Long inbound messages get truncated rather than dropped — the opening of a
# candidate's message carries the signal, and losing the whole message loses
# more than losing its tail.
MAX_INTERACTION_CHARS = 900


ROLE_STATEMENT = """\
You are an analyst supporting a recruiting team at an Indian technology
company. Candidates here have accepted an offer and are serving 30-90 days of
notice at their current employer before they join. A large share of them never
show up on day one, and the recruiter usually cannot see it coming.

Your job is triage: read what actually happened between this company and one
candidate, and tell the recruiter what they need to know. You never contact
the candidate. A human reads everything you produce and decides what to do."""


EVIDENCE_RULES = """\
HOW TO WEIGH THE EVIDENCE — this matters more than anything else here:

1. INBOUND email and WhatsApp messages are the candidate's own words, quoted
   verbatim. This is your strongest evidence. Weigh it above everything else.
   Read it closely and literally: hedges ("should be fine", "will confirm
   once I know"), unexplained delays, sudden vagueness about dates, and
   mentions of conversations with their current employer are signal, even
   when the message is friendly. Candidates about to withdraw are almost
   always polite; politeness is not reassurance.

2. CALL NOTES are a recruiter's paraphrase, typed from memory after the call
   ended. They are second-hand and optimistic by default — a recruiter who
   wants the hire to land hears reassurance. Treat them as weaker evidence
   than the candidate's own words. Where a call note and an inbound message
   disagree, the inbound message wins and you should say so.

3. OUTBOUND messages tell you what this company has said and offered. They
   are context, not evidence about the candidate's intent. What they do show
   is silence: an outbound message with no inbound reply after it is
   meaningful.

4. Structured fields on a call note (blocker raised, recruiter's read,
   whether the start date was confirmed) are the recruiter's own judgement,
   already deliberately recorded. Trust them more than the note's prose, but
   still below the candidate's own words.

Quote the candidate's actual words when you cite evidence. A recruiter has to
be able to see the sentence you drew a conclusion from, verbatim, in the
interaction log."""


GUARDRAILS = """\
HARD CONSTRAINTS — these override every other instruction:

* Never invent, imply, hint at, or ask about a change to compensation, salary,
  bonus, joining bonus, or any number. You do not know what was offered and
  you have no authority to change it.
* Never invent, imply, or propose a change to the start date or joining date,
  in either direction. Do not offer to delay it, bring it forward, or
  "look into flexibility" on it.
* Never propose a change to the role, title, level, team, or scope.
* Never promise, offer, or hint at anything this company has not already
  offered in an outbound message you can see in the context below. No
  "we can definitely sort that out", no "I'm sure we can work something out".
* Use only facts present in the context below. Do not invent an interaction,
  a concern, a family situation, a competing company, or a detail about the
  candidate's life that is not written there.
* If the evidence is thin, say so with LOW CONFIDENCE and a neutral
  assessment. Do not manufacture a plausible-sounding concern to fill the
  space. "Not enough contact to tell" is a genuinely useful answer here; a
  fabricated concern sends a recruiter into a phone call with a wrong premise
  and is worse than no answer.

Return only JSON matching the requested schema. No markdown, no code fences,
no commentary before or after the JSON."""


# ---------------------------------------------------------------------------
# Context rendering
# ---------------------------------------------------------------------------

_CHANNEL_LABELS = {
    InteractionChannel.EMAIL: "Email",
    InteractionChannel.WHATSAPP: "WhatsApp",
    InteractionChannel.CALL: "Phone call",
    InteractionChannel.IN_PERSON: "In person",
}

# The tag printed against each interaction, so the weighting rules above stay
# attached to the text they apply to.
_TIER_1 = "TIER 1 EVIDENCE - the candidate's own words, verbatim"
_TIER_2 = "TIER 2 - recruiter's paraphrase, written from memory after the call"
_TIER_3 = "context - what this company sent"


def _interaction_tier(interaction: Interaction) -> str:
    if interaction.direction == InteractionDirection.INBOUND:
        if interaction.channel in (InteractionChannel.EMAIL, InteractionChannel.WHATSAPP):
            return _TIER_1
        return _TIER_2
    if interaction.channel in (InteractionChannel.CALL, InteractionChannel.IN_PERSON):
        return _TIER_2
    return _TIER_3


def _render_interaction(index: int, interaction: Interaction, today: date) -> str:
    days_ago = (today - interaction.occurred_at.date()).days
    header = (
        f"[{index}] {interaction.occurred_at.date().isoformat()} "
        f"({days_ago} days ago) | {_CHANNEL_LABELS[interaction.channel]} | "
        f"{interaction.direction.value.upper()} | {_interaction_tier(interaction)}"
    )

    lines = [header]

    if interaction.channel == InteractionChannel.CALL:
        structured = [
            f"blocker raised: {interaction.blocker_category.value}"
            if interaction.blocker_raised and interaction.blocker_category is not None
            else "blocker raised: no",
        ]
        if interaction.recruiter_read is not None:
            structured.append(f"recruiter's read: {interaction.recruiter_read.value}")
        if interaction.date_confirmed is not None:
            structured.append(
                f"start date confirmed on the call: {'yes' if interaction.date_confirmed else 'no'}"
            )
        lines.append("    recruiter's structured read -> " + " | ".join(structured))

    content = interaction.content.strip()
    if len(content) > MAX_INTERACTION_CHARS:
        content = content[:MAX_INTERACTION_CHARS] + " [...truncated]"
    lines.append(f'    "{content}"')

    return "\n".join(lines)


def _render_interactions(interactions: list[Interaction], today: date) -> str:
    """Oldest first, so the model reads the relationship in the order it
    happened and the most recent message — the one that matters most — is the
    last thing before the question."""
    if not interactions:
        return (
            "INTERACTION HISTORY: none. This candidate has never been contacted and "
            "has never written in. There is no evidence of intent either way — say so "
            "rather than inferring one."
        )

    recent = sorted(interactions, key=lambda i: i.occurred_at)[-MAX_INTERACTIONS_IN_PROMPT:]
    omitted = len(interactions) - len(recent)
    heading = f"INTERACTION HISTORY ({len(recent)} shown, oldest first"
    heading += f", {omitted} older omitted):" if omitted else "):"

    return "\n".join([heading, ""] + [_render_interaction(i + 1, x, today) for i, x in enumerate(recent)])


def _render_stages(stages: list[CandidateStage], today: date) -> str:
    if not stages:
        return "ONBOARDING JOURNEY: no stages recorded."

    ordered = sorted(stages, key=lambda cs: cs.stage.sequence_order)
    lines = ["ONBOARDING JOURNEY (the engagement steps this company owes the candidate):"]
    for candidate_stage in ordered:
        marker = {
            StageStatus.COMPLETED: "[done]",
            StageStatus.SKIPPED: "[skipped]",
            StageStatus.IN_PROGRESS: "[in progress]",
            StageStatus.PENDING: "[pending]",
        }[candidate_stage.status]

        note = ""
        if candidate_stage.status in (StageStatus.PENDING, StageStatus.IN_PROGRESS):
            overdue = (today - candidate_stage.due_date).days
            if overdue > 0:
                note = f"  <-- OVERDUE by {overdue} days"

        lines.append(
            f"  {marker:<14} {candidate_stage.stage.label:<30} "
            f"due {candidate_stage.due_date.isoformat()}{note}"
        )
    return "\n".join(lines)


def build_candidate_context(
    candidate: Candidate,
    stages: list[CandidateStage],
    interactions: list[Interaction],
    floor: RuleFloor,
    today: date,
) -> str:
    """Render everything the model is allowed to know about one candidate.

    The rule-engine numbers are included deliberately. They are cheap,
    deterministic facts the model would otherwise have to infer from dates
    and get wrong — and stating them plainly frees the model to do the thing
    it is actually better at than the rules: reading what the candidate wrote.
    """
    notice_days = (candidate.joining_date - candidate.offer_date).days
    days_to_joining = floor.days_to_joining
    joining_phrase = (
        f"in {days_to_joining} days"
        if days_to_joining > 0
        else ("today" if days_to_joining == 0 else f"{abs(days_to_joining)} days ago (already passed)")
    )

    contact_line = (
        f"Last contact of any kind: {floor.days_since_contact} days ago"
        if candidate.last_interaction_at is not None
        else (
            f"Never contacted. It has been {floor.days_since_contact} days since the "
            "offer was accepted and nobody from this company has reached out."
        )
    )

    header = f"""\
CANDIDATE
  Name: {candidate.name}
  Role: {candidate.role} ({candidate.department})
  Location: {candidate.location}
  Offer accepted: {candidate.offer_date.isoformat()}
  Joining date: {candidate.joining_date.isoformat()} - {joining_phrase}
  Notice period: {notice_days} days
  Current engagement stage: {candidate.engagement_status.value}
  {contact_line}
  Today's date: {today.isoformat()}

RULE-ENGINE READING (deterministic, computed from dates only - it cannot read
any message content, which is why you are being asked):
  Rule-floor risk level: {floor.level.value}
  Days of silence: {floor.days_since_contact}
  Most overdue open journey stage: {floor.max_stage_overdue_days} days
  Blocker signal logged by the recruiter on a call: {floor.blocker_signal.value}"""

    recruiter_notes = (
        f"\n\nRECRUITER'S PRIVATE NOTES ON THIS CANDIDATE:\n  {candidate.notes.strip()}"
        if candidate.notes
        else ""
    )

    return "\n\n".join(
        [
            header + recruiter_notes,
            _render_stages(stages, today),
            _render_interactions(interactions, today),
        ]
    )


def _assemble(task: str, context: str, instructions: str) -> str:
    return "\n\n".join(
        [
            ROLE_STATEMENT,
            f"TASK: {task}",
            "=" * 70,
            context,
            "=" * 70,
            EVIDENCE_RULES,
            instructions,
            GUARDRAILS,
        ]
    )


# ---------------------------------------------------------------------------
# The four task prompts
# ---------------------------------------------------------------------------


def risk_assessment_prompt(context: str) -> str:
    return _assemble(
        "Assess how likely this candidate is to not show up on their joining date.",
        context,
        """\
WHAT TO RETURN:

  risk_level        "low", "medium", or "high".
                    high   - concrete evidence they may not join: a competing
                             conversation with their current employer, a
                             notice-period dispute they cannot resolve, an
                             unresolved blocker close to the joining date, or
                             a long unexplained silence with the date near.
                    medium - a real but manageable concern, or a worrying
                             pattern that is not yet a blocker.
                    low    - nothing in the evidence suggests a problem.

                    Deliberately lean toward flagging when you are unsure. A
                    false alarm costs the recruiter one phone call. A missed
                    one costs the hire, and the seat stays empty for months.

  confidence        0.0 to 1.0. This is your confidence in the level you just
                    gave, and it must reflect how much real evidence you had.
                    Two inbound messages that plainly state a problem: high.
                    Silence and nothing else: low, because silence is
                    genuinely ambiguous - a candidate heads-down on a handover
                    and a candidate who has already decided to stay look
                    identical from here.

  signals           Up to 5 pieces of evidence, strongest first. Quote the
                    candidate's own words where they exist - a short verbatim
                    fragment in quotation marks, copied exactly from an
                    interaction above. A recruiter reads these to decide
                    whether to trust your level, so a signal they cannot find
                    in the log is worse than no signal. Where the evidence is
                    an absence, say what is absent ("no reply to the 12 Aug
                    check-in, 18 days").

  reasoning         Under 500 characters, written for a recruiter about to
                    pick up the phone. Say what you think is happening and
                    why. If a cheerful call note contradicts a worried inbound
                    message, name that contradiction explicitly.

  concern_category  The single dominant concern, or "none" if there is no
                    evidence of one. Do not pick a category just to avoid
                    "none".""",
    )


def interaction_summary_prompt(context: str) -> str:
    return _assemble(
        "Summarise this candidate's engagement history for a recruiter who is "
        "about to call them and has thirty seconds to get up to speed.",
        context,
        """\
WHAT TO RETURN:

  summary           Under 800 characters. What has actually happened between
                    this company and this candidate, in plain prose and in
                    order. Lead with what the candidate has said about their
                    own situation. Note who has gone quiet and for how long.
                    Do not editorialise and do not repeat the whole log.

  key_concerns      Things the candidate has raised or implied that are not
                    settled. Draw them from the candidate's own words first.
                    Empty list if they have raised nothing - do not invent an
                    item to avoid an empty list.

  sentiment         The candidate's tone across their own inbound messages,
                    not the recruiter's tone and not your read of the
                    situation overall. "neutral" for a candidate who has sent
                    nothing to judge.

  unresolved_items  Concrete open loops: a question nobody answered, a
                    document nobody sent, a promised update that never came,
                    an overdue journey stage. Each one should be something a
                    recruiter could act on today.""",
    )


def next_action_prompt(context: str) -> str:
    return _assemble(
        "Recommend the single next thing this recruiter should do about this candidate.",
        context,
        """\
WHAT TO RETURN:

  action_type            One action, the most useful one. Not a plan.
                         schedule_call       - something needs a real
                                               conversation. Anything the
                                               candidate is weighing up
                                               belongs here, not in a message;
                                               a written nudge to a wavering
                                               candidate makes things worse.
                         send_message        - a light touch is enough.
                         escalate_to_manager - the hiring manager's
                                               involvement would change the
                                               outcome, e.g. role scope or a
                                               candidate who has stopped
                                               responding to the recruiter.
                         send_documents      - the blocker is paperwork.
                         no_action_needed    - genuinely fine and recently in
                                               contact. Use it when it is
                                               true; a recruiter with 40
                                               candidates needs the list to
                                               be short.

  channel                Where to do it. Match how this candidate actually
                         replies - if they answer on WhatsApp and ignore
                         email, that is the channel.

  urgency                How soon this matters, given how close the joining
                         date is.

  rationale              Why this action and not another, in two or three
                         sentences, citing the evidence it rests on.

  suggested_timing_days  0 for today, up to 30. 0 for anything urgent.""",
    )


def draft_message_prompt(context: str, channel: str, intent: str, tone: str) -> str:
    channel_rules = (
        """\
CHANNEL: email. Return a subject line in "subject" - it is required. Keep the
body to three short paragraphs at most. Open with the reason for writing, not
with pleasantries."""
        if channel == "email"
        else """\
CHANNEL: whatsapp. "subject" MUST be null - WhatsApp has no subject line.
Keep the body under 700 characters and write it as a chat message a person
would actually type: short, warm, no salutation block, no sign-off block, no
bullet points."""
    )

    return _assemble(
        "Draft a message for the RECRUITER to review, edit, and send. You are "
        "not sending it and it will not go out as written.",
        context,
        f"""\
{channel_rules}

WHAT THE RECRUITER WANTS THIS MESSAGE TO DO:
  {intent}

TONE: {tone}

HOW TO WRITE IT:

* Write as the named recruiter would - one person to another, not a company
  to a user. No "we value your candidature", no "reaching out to touch base".
* Personalise from the context: their name, their role, something specific
  they actually said. Generic warmth reads as a template and a candidate who
  is already wavering will spot it immediately.
* If they raised a concern, acknowledge that specific concern in their own
  terms. Do not solve it, do not offer anything, do not minimise it. Say it
  is heard and ask to talk about it.
* Never ask a question that pressures them to confirm they are still joining.
  "Are you still joining?" invites the answer nobody wants and forces a
  decision they may not have made. Ask about the thing, not the outcome.
* Do not mention risk scores, internal assessments, or that this candidate has
  been flagged. The candidate must never be able to tell that a system is
  watching them.

WHAT TO RETURN:

  channel               Exactly "{channel}".
  subject               {"A specific subject line, under 60 characters." if channel == "email" else "null. Not an empty string - null."}
  body                  The message itself. Plain text, ready for a human to
                        edit. No placeholders like [Name] - the details are
                        all in the context above, use them.
  tone                  "{tone}".
  personalization_used  What you drew on, e.g. "candidate's relocation concern
                        from 24 Aug WhatsApp", "role: Data Scientist". This is
                        how the recruiter checks you did not invent anything.""",
    )


def repair_prompt(original_prompt: str, raw_response: str, error: str) -> str:
    """The one retry. The model gets its own output back plus the exact
    validator complaint, which is far more likely to land than simply asking
    again — most failures are a shape error the model can see and fix once it
    is told precisely what broke."""
    return f"""\
{original_prompt}

{"=" * 70}
YOUR PREVIOUS RESPONSE WAS REJECTED. Fix it and return valid JSON.

What you returned:
{raw_response[:2000]}

Why it was rejected:
{error[:1500]}

Return the corrected JSON object only. No markdown, no code fences, no
explanation. Fix only what the error describes - keep the rest of your
assessment as it was, and do not weaken or change your conclusions to make
the shape easier."""
