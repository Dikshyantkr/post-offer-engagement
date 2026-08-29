"""Post-generation scrubbing of text that a candidate might actually read.

The prompt tells the model not to invent compensation or start-date changes.
This is the belt to that braces: prompts are instructions, not guarantees,
and one drafted message that says "we can look at the numbers again" turns an
AI convenience feature into a commitment the company did not make.

SCOPE — this runs on drafted messages ONLY, never on reasoning, rationale, or
summary text.

That distinction is deliberate and worth stating plainly. A drafted message
is outbound: a candidate may read it, so an invented promise in it is a real
liability. Reasoning and summaries are inbound: only the recruiter reads
them, and "he is waiting on his current employer's counter-offer number" is
exactly the evidence the recruiter needs to see. Scrubbing the word
"compensation" out of an analysis would delete the finding and leave the
recruiter with a risk badge and no reason for it — which is the failure mode
CLAUDE.md calls out as reading like magic.
"""

from __future__ import annotations

import logging
import re

from app.ai.contracts import DraftedMessage

logger = logging.getLogger(__name__)

# Any mention of money in an outbound draft is out of bounds. The recruiter
# owns that conversation; the model has no idea what was offered and no
# authority to revisit it.
_COMPENSATION = re.compile(
    r"""(?ix)
    \b(
        salary | compensation | ctc | remuneration | payout | paycheck
      | hike | increment | appraisal | bonus | esop | equity | stock
      | package | \bpay\b | \bcompensate
      | lpa | lakhs? | crores? | rupees
    )\b
    | ₹ | \bINR\b | \bRs\.? \s* \d | \$ \s* \d
    """
)

# Only a *change* to the date is forbidden. A draft that says "looking forward
# to 15 September" is repeating a fact from the context and is fine; one that
# says "we could push your start date" is inventing an offer.
_DATE_CHANGE = re.compile(
    r"""(?ix)
    \b(
        prepone[ds]? | postpone[ds]? | reschedul\w+
      | (push|move|shift|change|revise|adjust|delay|defer|extend|advance)\w*
        \s+ (?: \w+ \s+ ){0,3} (start|joining|onboarding|notice) \s+ (date|day|period)
      | (start|joining) \s+ date \s+ (?: \w+ \s+ ){0,3}
        (chang\w+ | mov\w+ | push\w+ | flexib\w+ | revis\w+ | later | earlier | delay\w*)
      | new \s+ (start|joining) \s+ date
      | flexib\w+ \s+ (?: \w+ \s+ ){0,3} (start|joining) \s+ date
      | (later|earlier) \s+ (start|joining) \s+ date
    )\b
    """
)

# Commitment language. The model may acknowledge a concern; it may not resolve
# one on the company's behalf.
_UNBACKED_PROMISE = re.compile(
    r"""(?ix)
    \b(
        guarantee\w* | i \s+ promise | we \s+ promise
      | rest \s+ assured \s+ (?: that \s+ )? (?: we|i ) \s+ (?:will|can|'ll)
      | (?:we|i) \s+ (?:can|will|'ll|could) \s+ (?: certainly|definitely|absolutely|surely )
      | (?:we|i) \s+ (?:can|will|'ll) \s+ (?: match | beat | revise | increase | improve | sort \s+ that )
      | (?:we|i) \s+ (?:am|are) \s+ (?:sure|confident) \s+ (?:we|i) \s+ can
    )\b
    """
)

_RULES = (
    ("compensation", _COMPENSATION),
    ("start-date change", _DATE_CHANGE),
    ("unbacked promise", _UNBACKED_PROMISE),
)

# Split on sentence enders, keeping the punctuation with the sentence it ends.
# Sentence granularity is the right unit: dropping the whole message over one
# bad clause throws away a usable draft, and dropping sub-sentence fragments
# leaves ungrammatical wreckage a recruiter has to repair by hand.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _violation(text: str) -> str | None:
    for label, pattern in _RULES:
        if pattern.search(text):
            return label
    return None


def scrub_text(text: str) -> tuple[str, list[str]]:
    """Return (cleaned text, descriptions of what was removed).

    Paragraph breaks survive; a paragraph left empty by scrubbing is dropped
    so the result does not read as though something was cut out of it.
    """
    removed: list[str] = []
    kept_paragraphs: list[str] = []

    for paragraph in text.split("\n\n"):
        kept_lines: list[str] = []
        for line in paragraph.split("\n"):
            if not line.strip():
                kept_lines.append(line)
                continue

            kept_sentences = []
            for sentence in _SENTENCE_SPLIT.split(line):
                label = _violation(sentence)
                if label is None:
                    kept_sentences.append(sentence)
                else:
                    removed.append(f"{label}: {sentence.strip()}")

            if kept_sentences:
                kept_lines.append(" ".join(kept_sentences))

        remainder = "\n".join(kept_lines).strip()
        if remainder:
            kept_paragraphs.append(remainder)

    return "\n\n".join(kept_paragraphs), removed


def scrub_drafted_message(
    message: DraftedMessage, fallback_subject: str
) -> tuple[DraftedMessage, list[str], bool]:
    """Scrub a drafted message.

    Returns (message, removed fragments, body_was_gutted). `body_was_gutted`
    is True when scrubbing left nothing usable — the caller substitutes the
    deterministic template and flags the result as a fallback, because what
    comes back is then no longer the model's message and saying otherwise
    would be a lie told to a recruiter about to press send.
    """
    body, removed = scrub_text(message.body)

    subject = message.subject
    if subject is not None and _violation(subject) is not None:
        removed.append(f"subject: {subject}")
        subject = fallback_subject

    gutted = not body.strip()
    if gutted:
        logger.warning(
            "Guardrails removed the entire drafted message body (%d fragments); "
            "falling back to the deterministic template",
            len(removed),
        )
        return message, removed, True

    if removed:
        logger.warning("Guardrails stripped %d fragment(s) from a drafted message", len(removed))

    return (
        message.model_copy(update={"body": body, "subject": subject}),
        removed,
        False,
    )
