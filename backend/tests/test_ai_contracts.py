"""Contract and guardrail tests — pure, no database, no provider.

These cover the part of Module 5 that decides whether an LLM response is
allowed into the application at all. The engine's repair-and-fallback
machinery is only as valuable as the contracts are strict, so the validators
are tested directly here rather than only through the pipeline.
"""

from __future__ import annotations

import pytest
from google.genai import types
from pydantic import ValidationError

from app.ai.contracts import (
    WHATSAPP_BODY_MAX_CHARS,
    DraftedMessage,
    InteractionSummary,
    NextAction,
    RiskAssessment,
    json_schema_for,
)
from app.ai.guardrails import scrub_drafted_message, scrub_text

# ---------------------------------------------------------------------------
# DraftedMessage: the subject/channel rule and the WhatsApp cap
# ---------------------------------------------------------------------------


def _draft(**overrides) -> dict:
    payload = {
        "channel": "email",
        "subject": "Quick check-in before your start date",
        "body": "Hi Sana, hope the handover is going smoothly.",
        "tone": "warm",
        "personalization_used": ["first name"],
    }
    payload.update(overrides)
    return payload


def test_email_without_a_subject_is_rejected():
    with pytest.raises(ValidationError, match="subject is required when channel is 'email'"):
        DraftedMessage.model_validate(_draft(subject=None))


def test_whatsapp_with_a_subject_is_rejected():
    with pytest.raises(ValidationError, match="subject must be null when channel is 'whatsapp'"):
        DraftedMessage.model_validate(_draft(channel="whatsapp", subject="Checking in"))


def test_whatsapp_body_over_the_cap_is_rejected():
    with pytest.raises(ValidationError, match="at most 700 characters"):
        DraftedMessage.model_validate(
            _draft(channel="whatsapp", subject=None, body="x" * (WHATSAPP_BODY_MAX_CHARS + 1))
        )


def test_whatsapp_body_at_the_cap_is_accepted():
    message = DraftedMessage.model_validate(
        _draft(channel="whatsapp", subject=None, body="x" * WHATSAPP_BODY_MAX_CHARS)
    )
    assert message.subject is None


def test_blank_subject_on_whatsapp_is_treated_as_absent():
    """Structured-output modes routinely emit "" instead of null for a field
    they have nothing to say about. Failing over that would spend the single
    repair attempt on a difference with no meaning."""
    message = DraftedMessage.model_validate(_draft(channel="whatsapp", subject="   "))
    assert message.subject is None


def test_blank_subject_on_email_still_fails():
    """The normalisation must not become a loophole: an email with a
    whitespace subject is an email with no subject."""
    with pytest.raises(ValidationError, match="subject is required"):
        DraftedMessage.model_validate(_draft(subject="  "))


def test_email_body_is_not_length_capped():
    """The cap is a WhatsApp-shaped rule, not a general one."""
    assert DraftedMessage.model_validate(_draft(body="x" * 5000)).channel == "email"


# ---------------------------------------------------------------------------
# The other three contracts
# ---------------------------------------------------------------------------


def _risk(**overrides) -> dict:
    payload = {
        "risk_level": "high",
        "confidence": 0.8,
        "signals": ['"not sure what it\'s about tbh"'],
        "reasoning": "Current employer called her in right after she resigned.",
        "concern_category": "counter_offer",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("bad", [-0.1, 1.1])
def test_confidence_outside_zero_to_one_is_rejected(bad: float):
    with pytest.raises(ValidationError):
        RiskAssessment.model_validate(_risk(confidence=bad))


def test_more_than_five_signals_is_rejected():
    with pytest.raises(ValidationError):
        RiskAssessment.model_validate(_risk(signals=[f"s{i}" for i in range(6)]))


def test_reasoning_over_500_chars_is_rejected():
    with pytest.raises(ValidationError):
        RiskAssessment.model_validate(_risk(reasoning="x" * 501))


def test_invented_risk_level_is_rejected():
    """A model that answers "critical" gets repaired, not silently coerced —
    the enum is the whole reason the level can be trusted downstream."""
    with pytest.raises(ValidationError):
        RiskAssessment.model_validate(_risk(risk_level="critical"))


def test_invented_concern_category_is_rejected():
    with pytest.raises(ValidationError):
        RiskAssessment.model_validate(_risk(concern_category="visa"))


def test_next_action_timing_is_bounded():
    with pytest.raises(ValidationError):
        NextAction.model_validate(
            {
                "action_type": "send_message",
                "channel": "email",
                "urgency": "low",
                "rationale": "ok",
                "suggested_timing_days": 45,
            }
        )


def test_interaction_summary_caps_the_summary_length():
    with pytest.raises(ValidationError):
        InteractionSummary.model_validate(
            {
                "summary": "x" * 801,
                "key_concerns": [],
                "sentiment": "neutral",
                "unresolved_items": [],
            }
        )


# ---------------------------------------------------------------------------
# Schema generation — what the provider is actually asked for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "contract", [RiskAssessment, NextAction, DraftedMessage, InteractionSummary]
)
def test_every_contract_produces_a_provider_acceptable_schema(contract):
    """Generated from the model, so a field added to a contract cannot leave
    the provider being asked for the old shape."""
    schema = json_schema_for(contract)

    assert schema["type"] == "object"
    assert set(schema["properties"]) == set(contract.model_fields)
    assert schema["propertyOrdering"] == list(contract.model_fields)

    # The real check: the provider SDK accepts it.
    types.Schema.model_validate(schema)


def test_optional_field_becomes_nullable_rather_than_a_null_type():
    """`str | None` is anyOf[string, null] in JSON Schema, and a "null" type
    is not something these schemas have — it has to become the nullable flag
    or the provider rejects the whole request."""
    subject = json_schema_for(DraftedMessage)["properties"]["subject"]
    assert subject == {"type": "string", "nullable": True}


def test_literals_become_enums():
    assert json_schema_for(RiskAssessment)["properties"]["risk_level"]["enum"] == [
        "low",
        "medium",
        "high",
    ]


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


def test_compensation_sentence_is_stripped_and_the_rest_survives():
    cleaned, removed = scrub_text(
        "Hi Sana, great to hear from you. We can definitely revise the salary if that helps. "
        "Let me know if a call this week works."
    )
    assert "salary" not in cleaned
    assert "great to hear from you" in cleaned
    assert "Let me know if a call this week works." in cleaned
    assert len(removed) == 1


def test_start_date_change_is_stripped():
    cleaned, removed = scrub_text("Happy to push your joining date back if you need more time.")
    assert cleaned == ""
    assert removed and "start-date change" in removed[0]


def test_mentioning_the_start_date_without_changing_it_survives():
    """The rule is 'no invented changes', not 'never mention the date'. A draft
    that cannot refer to the start date at all is useless for the one job it
    has."""
    text = "Looking forward to having you with us on 15 September."
    cleaned, removed = scrub_text(text)
    assert cleaned == text
    assert removed == []


def test_unbacked_promise_is_stripped():
    cleaned, removed = scrub_text("I promise we will sort this out for you.")
    assert cleaned == ""
    assert removed


def test_acknowledging_a_concern_is_not_a_promise():
    text = (
        "I hear you on the housing situation in Pune, that sounds stressful. "
        "Would you like to talk it through this week?"
    )
    cleaned, removed = scrub_text(text)
    assert cleaned == text
    assert removed == []


def test_paragraph_structure_survives_scrubbing():
    cleaned, _ = scrub_text("Hi Sana.\n\nWe can increase the package.\n\nTalk soon.")
    assert cleaned == "Hi Sana.\n\nTalk soon."


def test_scrubbing_a_drafted_message_replaces_a_bad_subject():
    message = DraftedMessage.model_validate(
        _draft(subject="Revised compensation for you", body="Hi Sana, hope all is well.")
    )
    scrubbed, removed = scrub_drafted_message(message, fallback_subject="Checking in")[:2]

    assert scrubbed.subject == "Checking in"
    assert any(r.startswith("subject:") for r in removed)
    assert scrubbed.body == "Hi Sana, hope all is well."


def test_a_message_that_is_entirely_violations_is_reported_as_gutted():
    """Nothing usable survives, so the caller must substitute the
    deterministic template and flag the result — returning an empty body, or
    calling what is left "the model's message", would both be wrong."""
    message = DraftedMessage.model_validate(
        _draft(subject="Hello", body="We can definitely match your current salary.")
    )
    _scrubbed, removed, gutted = scrub_drafted_message(message, fallback_subject="Checking in")

    assert gutted is True
    assert removed
