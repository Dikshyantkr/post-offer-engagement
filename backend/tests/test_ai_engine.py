"""Module 5 pipeline tests, end to end through the API with a FakeProvider.

No test in this file touches the real Gemini API. Everything the LLM might do
— return good JSON, return garbage, return garbage twice, raise, return a
message promising a raise — is queued on the fake and asserted against.

The two things worth being strict about, and the reason this file is written
against the HTTP layer rather than the engine directly:

* the failure paths must produce a 200 with a flag, not a 500. That is only
  provable through the app's exception handling.
* an AI assessment must never lower a risk level. That property lives in the
  interaction between engine, ai_service and risk_service, so testing any one
  of them in isolation would not show it holds.

Every candidate here is created through `make_candidate`, so the seeded 54 and
their analyses are untouched (see conftest's _no_row_leaks guard).
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.ai.provider import LLMProvider
from app.db import SessionLocal
from app.enums import RiskLevel, RiskSource, ValidationStatus
from app.errors import LLMProviderError
from app.main import app
from app.models import AIAnalysis, AuditLog, Candidate
from app.routers.ai import get_llm_provider
from tests.helpers import API

TODAY = date.today()


# ---------------------------------------------------------------------------
# The fake provider
# ---------------------------------------------------------------------------


class ProviderCalledTooOften(BaseException):
    """Deliberately a BaseException, not an Exception.

    The engine catches Exception around every provider call and degrades to
    its fallback — correct behaviour, and it would silently swallow this and
    hide the exact thing the repair tests exist to assert. A BaseException
    escapes the engine and fails the test loudly.
    """


class FakeProvider(LLMProvider):
    """Returns queued responses in order. An `Exception` in the queue is
    raised instead of returned, so provider failures are expressed the same
    way as provider successes.

    Running past the end of the queue is an error, not a repeat: "exactly one
    repair attempt" is only a real assertion if a third call fails.
    """

    name = "fake"

    def __init__(self, *responses: str | Exception, model_name: str = "fake-model-1") -> None:
        self.model_name = model_name
        self._responses = list(responses)
        self.prompts: list[str] = []
        self.schemas: list[dict] = []

    @property
    def call_count(self) -> int:
        return len(self.prompts)

    def generate_json(self, prompt: str, schema_hint: dict) -> tuple[str, int]:
        index = len(self.prompts)
        self.prompts.append(prompt)
        self.schemas.append(schema_hint)

        if index >= len(self._responses):
            raise ProviderCalledTooOften(
                f"provider called {index + 1} times, but only {len(self._responses)} "
                "responses were queued"
            )

        response = self._responses[index]
        if isinstance(response, Exception):
            raise response
        return response, 7


@pytest.fixture
def use_provider():
    """Swap the provider for the duration of one test via FastAPI's
    dependency_overrides — the real GeminiProvider is never constructed."""
    installed: list[FakeProvider] = []

    def _use(*responses: str | Exception) -> FakeProvider:
        provider = FakeProvider(*responses)
        app.dependency_overrides[get_llm_provider] = lambda: provider
        installed.append(provider)
        return provider

    yield _use
    app.dependency_overrides.pop(get_llm_provider, None)


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def risk_json(level: str = "high", **overrides) -> str:
    payload = {
        "risk_level": level,
        "confidence": 0.82,
        "signals": ['"they\'ve called me in for a chat tomorrow"'],
        "reasoning": "Current employer initiated a conversation immediately after she resigned.",
        "concern_category": "counter_offer",
    }
    payload.update(overrides)
    return json.dumps(payload)


def summary_json(**overrides) -> str:
    payload = {
        "summary": "Two outbound check-ins, one inbound reply flagging a meeting with her employer.",
        "key_concerns": ["Possible counter-offer"],
        "sentiment": "concerned",
        "unresolved_items": ["Outcome of the meeting with her current employer"],
    }
    payload.update(overrides)
    return json.dumps(payload)


def next_action_json(**overrides) -> str:
    payload = {
        "action_type": "schedule_call",
        "channel": "call",
        "urgency": "high",
        "rationale": "A counter-offer conversation needs a call, not a message.",
        "suggested_timing_days": 0,
    }
    payload.update(overrides)
    return json.dumps(payload)


def draft_json(**overrides) -> str:
    payload = {
        "channel": "email",
        "subject": "Checking in before your start date",
        "body": "Hi Sana, hope the handover is going smoothly. Do you have time for a quick call?",
        "tone": "warm",
        "personalization_used": ["first name", "notice period"],
    }
    payload.update(overrides)
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Candidate fixtures with a known rule floor
# ---------------------------------------------------------------------------


def _medium_floor_candidate(make_candidate) -> dict:
    """Offered 2 days ago, joining in 80 days, never contacted: the first two
    journey stages are already overdue, so the rules land on MEDIUM (mirrors
    test_risk_api's backdated-offer case)."""
    return make_candidate(
        offer_date=(TODAY - timedelta(days=2)).isoformat(),
        joining_date=(TODAY + timedelta(days=80)).isoformat(),
    )


def _high_floor_candidate(make_candidate) -> dict:
    """Offered 40 days ago and never contacted — silent well past the 10-day
    HIGH threshold."""
    return make_candidate(
        offer_date=(TODAY - timedelta(days=40)).isoformat(),
        joining_date=(TODAY + timedelta(days=50)).isoformat(),
    )


def _db_candidate(candidate_id: str) -> Candidate:
    with SessionLocal() as db:
        return db.get(Candidate, uuid.UUID(candidate_id))


def _analyses(candidate_id: str) -> list[AIAnalysis]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(AIAnalysis)
                .where(AIAnalysis.candidate_id == uuid.UUID(candidate_id))
                .order_by(AIAnalysis.created_at)
            )
        )


def _assess(client, candidate_id: str, actor: str = "test-ai"):
    return client.post(
        f"{API}/ai/candidates/{candidate_id}/assess-risk", headers={"X-Actor": actor}
    )


# ---------------------------------------------------------------------------
# 1. The happy path: valid JSON parses and persists
# ---------------------------------------------------------------------------


def test_valid_json_parses_and_persists(client, make_candidate, use_provider):
    candidate = _medium_floor_candidate(make_candidate)
    raw = risk_json("high")
    provider = use_provider(raw)

    resp = _assess(client, candidate["id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert provider.call_count == 1
    assert body["meta"]["validation_status"] == "valid"
    assert body["meta"]["was_fallback"] is False
    assert body["meta"]["prompt_version"] == "v1"
    assert body["meta"]["model_name"] == "fake-model-1"
    assert body["assessment"]["risk_level"] == "high"
    assert body["assessment"]["concern_category"] == "counter_offer"

    rows = _analyses(candidate["id"])
    assert len(rows) == 1, "every call must be persisted, exactly once"
    row = rows[0]
    assert row.analysis_type == "risk_assessment"
    assert row.model_name == "fake-model-1"
    assert row.prompt_version == "v1"
    assert row.raw_response == raw
    assert row.parsed_output["reasoning"].startswith("Current employer")
    assert row.risk_level == RiskLevel.HIGH
    assert row.confidence == pytest.approx(0.82)
    assert row.validation_status == ValidationStatus.VALID
    assert row.was_fallback is False
    assert row.latency_ms == 7
    assert str(row.id) == body["meta"]["analysis_id"]


def test_the_prompt_carries_real_candidate_context_and_the_schema(client, make_candidate, use_provider):
    """A prompt that does not contain the candidate is a prompt that cannot
    have assessed them."""
    candidate = _medium_floor_candidate(make_candidate)
    provider = use_provider(risk_json("low"))

    _assess(client, candidate["id"])

    prompt = provider.prompts[0]
    assert candidate["name"] in prompt
    assert candidate["role"] in prompt
    assert candidate["location"] in prompt
    assert candidate["joining_date"] in prompt
    # The evidence-weighting instruction is the substance of the prompt.
    assert "TIER 1 EVIDENCE" in prompt or "the candidate's own words" in prompt
    assert "paraphrase" in prompt
    # And the guardrails travel with every prompt.
    assert "compensation" in prompt.lower()

    assert provider.schemas[0]["properties"]["risk_level"]["enum"] == ["low", "medium", "high"]


# ---------------------------------------------------------------------------
# 2. Malformed JSON -> exactly one repair attempt, then success
# ---------------------------------------------------------------------------


def test_malformed_json_triggers_exactly_one_repair_then_succeeds(
    client, make_candidate, use_provider
):
    candidate = _medium_floor_candidate(make_candidate)
    provider = use_provider("{ not json at all", risk_json("high"))

    resp = _assess(client, candidate["id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert provider.call_count == 2, "exactly one repair attempt, no more"
    assert body["meta"]["validation_status"] == "repaired"
    assert body["meta"]["was_fallback"] is False
    assert body["assessment"]["risk_level"] == "high"


def test_schema_violation_is_repaired_the_same_way_as_bad_syntax(
    client, make_candidate, use_provider
):
    """Well-formed JSON of the wrong shape is the more common failure and must
    take the same path."""
    candidate = _medium_floor_candidate(make_candidate)
    provider = use_provider(risk_json("critical"), risk_json("high"))

    body = _assess(client, candidate["id"]).json()

    assert provider.call_count == 2
    assert body["meta"]["validation_status"] == "repaired"
    assert body["assessment"]["risk_level"] == "high"


def test_the_repair_prompt_carries_the_validator_error_back_to_the_model(
    client, make_candidate, use_provider
):
    """Retrying with the same prompt would mostly reproduce the same mistake.
    The repair attempt is worth making because it tells the model exactly what
    broke."""
    candidate = _medium_floor_candidate(make_candidate)
    provider = use_provider(risk_json("critical"), risk_json("high"))

    _assess(client, candidate["id"])

    repair = provider.prompts[1]
    assert "YOUR PREVIOUS RESPONSE WAS REJECTED" in repair
    assert "critical" in repair, "the model must see what it returned"
    assert "risk_level" in repair, "and which field the validator rejected"


def test_every_attempt_is_kept_in_the_persisted_raw_response(
    client, make_candidate, use_provider
):
    candidate = _medium_floor_candidate(make_candidate)
    use_provider("{ not json at all", risk_json("high"))

    _assess(client, candidate["id"])

    rows = _analyses(candidate["id"])
    assert len(rows) == 1, "one row per engine call, not per provider call"
    assert "attempt 1" in rows[0].raw_response
    assert "attempt 2" in rows[0].raw_response
    assert "{ not json at all" in rows[0].raw_response, "the rejected text is not thrown away"


def test_latency_accumulates_across_both_attempts(client, make_candidate, use_provider):
    candidate = _medium_floor_candidate(make_candidate)
    use_provider("{ not json", risk_json("high"))

    body = _assess(client, candidate["id"]).json()

    assert body["meta"]["latency_ms"] == 14, "7ms per fake call, both attempts counted"


# ---------------------------------------------------------------------------
# 3. Twice invalid -> fallback, 200, flagged
# ---------------------------------------------------------------------------


def test_twice_invalid_falls_back_and_still_returns_200(client, make_candidate, use_provider):
    candidate = _medium_floor_candidate(make_candidate)
    provider = use_provider("{ not json", "still not json")

    resp = _assess(client, candidate["id"])
    assert resp.status_code == 200, "a misbehaving model must not break the app"
    body = resp.json()

    assert provider.call_count == 2, "two attempts, then stop — no third try"
    assert body["meta"]["was_fallback"] is True
    assert body["meta"]["validation_status"] == "failed"

    # The fallback is the deterministic rule floor, and says so.
    assert body["assessment"]["risk_level"] == body["risk"]["rule_floor_level"]
    assert "rule floor" in body["assessment"]["reasoning"]
    assert body["assessment"]["confidence"] < 0.5, "a rule floor is a weak signal, labelled as one"

    rows = _analyses(candidate["id"])
    assert len(rows) == 1
    assert rows[0].was_fallback is True
    assert rows[0].validation_status == ValidationStatus.FAILED
    assert rows[0].parsed_output["risk_level"] == body["assessment"]["risk_level"]


def test_the_fallback_still_names_the_evidence_it_has(client, make_candidate, use_provider):
    """A fallback with no signals is indistinguishable from a shrug. The rule
    inputs are real evidence and the recruiter should see them."""
    candidate = _high_floor_candidate(make_candidate)
    use_provider("nope", "nope")

    body = _assess(client, candidate["id"]).json()

    assert body["assessment"]["signals"], "the rule inputs are evidence too"
    assert any("contact" in s or "Never contacted" in s for s in body["assessment"]["signals"])


# ---------------------------------------------------------------------------
# 4. Provider exceptions fall back rather than 500
# ---------------------------------------------------------------------------


def test_provider_exception_falls_back_without_a_repair_attempt(
    client, make_candidate, use_provider
):
    """A repair prompt fixes a shape the model got wrong. A transport failure
    is not a shape problem, so the second call would just spend another few
    seconds failing the same way while a recruiter waits.

    Only one response is queued, so a second call would raise
    ProviderCalledTooOften and fail this test.
    """
    candidate = _medium_floor_candidate(make_candidate)
    provider = use_provider(RuntimeError("connection reset by peer"))

    resp = _assess(client, candidate["id"])

    assert resp.status_code == 200, "a dead provider must not surface as a 500"
    assert provider.call_count == 1
    assert resp.json()["meta"]["was_fallback"] is True


def test_llm_provider_error_falls_back_rather_than_returning_503(
    client, make_candidate, use_provider
):
    """errors.py maps LLMProviderError to 503. Module 5's rule wins over that
    for these endpoints: the caller gets a usable answer plus was_fallback,
    because triage that degrades is worth more than triage that stops."""
    candidate = _medium_floor_candidate(make_candidate)
    use_provider(LLMProviderError("GEMINI_API_KEY is not set"))

    resp = _assess(client, candidate["id"])

    assert resp.status_code == 200
    assert resp.json()["meta"]["was_fallback"] is True


def test_a_failed_call_is_still_persisted_with_its_error(client, make_candidate, use_provider):
    candidate = _medium_floor_candidate(make_candidate)
    use_provider(RuntimeError("connection reset by peer"))

    _assess(client, candidate["id"])

    rows = _analyses(candidate["id"])
    assert len(rows) == 1, "a failed call is still a call, and still gets a row"
    assert "connection reset by peer" in rows[0].raw_response
    assert "provider error" in rows[0].raw_response, (
        "the label is what distinguishes a stored exception from a stored model response"
    )
    assert rows[0].was_fallback is True


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        ("summarize", None),
        ("recommend-action", None),
        ("draft-message", {"channel": "email", "intent": "check in on the notice period"}),
    ],
)
def test_every_ai_endpoint_degrades_rather_than_failing(
    client, make_candidate, use_provider, endpoint, payload
):
    """The guarantee is the module's, not one endpoint's."""
    candidate = _high_floor_candidate(make_candidate)
    use_provider(RuntimeError("provider down"))

    resp = client.post(f"{API}/ai/candidates/{candidate['id']}/{endpoint}", json=payload)

    assert resp.status_code == 200, resp.text
    assert resp.json()["meta"]["was_fallback"] is True


# ---------------------------------------------------------------------------
# 5. The risk rule: AI may raise, never lower; HR beats both
# ---------------------------------------------------------------------------


def test_ai_high_raises_a_rule_floor_medium(client, make_candidate, use_provider):
    """The case Module 4 cannot see on its own: nothing in the dates justifies
    HIGH, and the candidate's messages do."""
    candidate = _medium_floor_candidate(make_candidate)
    use_provider(risk_json("high"))

    body = _assess(client, candidate["id"]).json()

    assert body["risk"]["rule_floor_level"] == "medium"
    assert body["risk"]["ai_level"] == "high"
    assert body["risk"]["final_level"] == "high"
    assert body["risk"]["risk_source"] == "ai"
    assert body["risk"]["raised_by_ai"] is True

    row = _db_candidate(candidate["id"])
    assert row.risk_level == RiskLevel.HIGH
    assert row.risk_source == RiskSource.AI
    # The floor is still recorded so the UI can show it beside the badge.
    assert 40.0 <= row.risk_score_base <= 69.0


def test_ai_low_does_not_lower_a_rule_floor_high(client, make_candidate, use_provider):
    """The load-bearing test of the whole module. A model that reads a silent
    candidate's empty inbox and concludes "seems fine" must not be able to
    talk the system out of worrying about them."""
    candidate = _high_floor_candidate(make_candidate)
    use_provider(risk_json("low", concern_category="none"))

    body = _assess(client, candidate["id"]).json()

    assert body["risk"]["rule_floor_level"] == "high"
    assert body["risk"]["ai_level"] == "low"
    assert body["risk"]["final_level"] == "high", "final = max(base, ai)"
    assert body["risk"]["risk_source"] == "rule"
    assert body["risk"]["raised_by_ai"] is False

    row = _db_candidate(candidate["id"])
    assert row.risk_level == RiskLevel.HIGH
    assert row.risk_source == RiskSource.RULE


def test_ai_medium_does_not_lower_a_rule_floor_high(client, make_candidate, use_provider):
    """Not just the extremes — anything below the floor loses."""
    candidate = _high_floor_candidate(make_candidate)
    use_provider(risk_json("medium"))

    body = _assess(client, candidate["id"]).json()

    assert body["risk"]["final_level"] == "high"
    assert _db_candidate(candidate["id"]).risk_level == RiskLevel.HIGH


def test_ai_matching_the_floor_leaves_the_source_as_rule(client, make_candidate, use_provider):
    """Agreement is not a raise. Labelling it 'ai' would overstate what the
    model contributed and would wrongly protect the level from the next
    nightly recompute."""
    candidate = _high_floor_candidate(make_candidate)
    use_provider(risk_json("high"))

    body = _assess(client, candidate["id"]).json()

    assert body["risk"]["final_level"] == "high"
    assert body["risk"]["risk_source"] == "rule"
    assert body["risk"]["raised_by_ai"] is False


def test_hr_override_wins_over_both_the_rules_and_the_ai(client, make_candidate, use_provider):
    candidate = _high_floor_candidate(make_candidate)

    # A human deliberately marks this silent candidate LOW.
    resp = client.patch(
        f"{API}/candidates/{candidate['id']}",
        json={"risk_level": "low"},
        headers={"X-Actor": "hr-lead"},
    )
    assert resp.json()["risk_source"] == "hr_override"

    use_provider(risk_json("high"))
    body = _assess(client, candidate["id"]).json()

    assert body["risk"]["applied"] is False
    assert body["risk"]["final_level"] == "low"
    assert body["risk"]["risk_source"] == "hr_override"

    row = _db_candidate(candidate["id"])
    assert row.risk_level == RiskLevel.LOW
    assert row.risk_source == RiskSource.HR_OVERRIDE
    # The assessment is still recorded — the human overrode it, they did not
    # erase it.
    assert len(_analyses(candidate["id"])) == 1


def test_an_ai_raise_survives_the_nightly_recompute(client, make_candidate, use_provider):
    """risk_service's skipped_ai_higher hook, exercised for real: without it a
    sweep would quietly undo every AI raise a few hours later."""
    candidate = _medium_floor_candidate(make_candidate)
    use_provider(risk_json("high"))
    _assess(client, candidate["id"])
    assert _db_candidate(candidate["id"]).risk_source == RiskSource.AI

    result = client.post(f"{API}/risk/recompute", headers={"X-Actor": "test-ai"}).json()

    assert result["skipped_ai_higher"] >= 1
    row = _db_candidate(candidate["id"])
    assert row.risk_level == RiskLevel.HIGH
    assert row.risk_source == RiskSource.AI


def test_the_raise_is_audited_with_both_levels(client, make_candidate, use_provider):
    """A badge that changed with no record of why is the failure CLAUDE.md
    calls out — the audit row has to carry the floor, the AI level, and the
    analysis they came from."""
    candidate = _medium_floor_candidate(make_candidate)
    use_provider(risk_json("high"))

    body = _assess(client, candidate["id"], actor="meera.iyer").json()

    with SessionLocal() as db:
        rows = list(
            db.scalars(
                select(AuditLog).where(
                    AuditLog.entity_id == uuid.UUID(candidate["id"]),
                    AuditLog.action == "ai_risk_assessment",
                )
            )
        )

    assert len(rows) == 1
    assert rows[0].actor == "meera.iyer"
    assert rows[0].after["rule_floor_level"] == "medium"
    assert rows[0].after["ai_level"] == "high"
    assert rows[0].after["analysis_id"] == body["meta"]["analysis_id"]


def test_a_fallback_assessment_cannot_raise_risk_on_its_own(
    client, make_candidate, use_provider
):
    """The fallback returns the rule floor, so it can never exceed it. Worth
    pinning: a fallback that raised risk would be the rules laundering
    themselves as an AI finding."""
    candidate = _medium_floor_candidate(make_candidate)
    use_provider("garbage", "garbage")

    body = _assess(client, candidate["id"]).json()

    assert body["risk"]["raised_by_ai"] is False
    assert body["risk"]["final_level"] == body["risk"]["rule_floor_level"]
    assert _db_candidate(candidate["id"]).risk_source == RiskSource.RULE


def test_assessing_a_joined_candidate_changes_nothing(client, make_candidate, use_provider):
    candidate = _high_floor_candidate(make_candidate)
    client.patch(f"{API}/candidates/{candidate['id']}", json={"final_outcome": "joined"})
    before = _db_candidate(candidate["id"])

    use_provider(risk_json("high"))
    body = _assess(client, candidate["id"]).json()

    assert body["risk"]["applied"] is False
    after = _db_candidate(candidate["id"])
    assert after.risk_level == before.risk_level
    assert after.risk_source == before.risk_source


# ---------------------------------------------------------------------------
# 6. Drafted messages and their guardrails
# ---------------------------------------------------------------------------


def _draft(client, candidate_id: str, **payload):
    body = {"channel": "email", "intent": "check in on the notice period"}
    body.update(payload)
    return client.post(f"{API}/ai/candidates/{candidate_id}/draft-message", json=body)


def test_draft_message_returns_the_model_output_unchanged_when_it_is_clean(
    client, make_candidate, use_provider
):
    candidate = _medium_floor_candidate(make_candidate)
    use_provider(draft_json())

    body = _draft(client, candidate["id"]).json()

    assert body["draft"]["subject"] == "Checking in before your start date"
    assert body["guardrails_removed"] == []
    assert body["meta"]["analysis_type"] == "drafted_message"


def test_guardrails_strip_an_invented_compensation_promise(
    client, make_candidate, use_provider
):
    """The prompt forbids it; this is the check that the prompt was obeyed.
    One drafted message offering money the company never offered turns a
    convenience feature into a commitment."""
    candidate = _medium_floor_candidate(make_candidate)
    use_provider(
        draft_json(
            body=(
                "Hi Sana, hope the handover is going smoothly. "
                "We can definitely match whatever salary they have offered you. "
                "Do you have time for a quick call this week?"
            )
        )
    )

    body = _draft(client, candidate["id"]).json()

    assert "salary" not in body["draft"]["body"]
    assert "quick call this week" in body["draft"]["body"], "the usable text survives"
    assert body["guardrails_removed"], "and the recruiter is told what was removed"


def test_guardrails_strip_an_invented_start_date_change(client, make_candidate, use_provider):
    candidate = _medium_floor_candidate(make_candidate)
    use_provider(
        draft_json(body="Hi Sana, hope all is well. We could push your joining date back a month.")
    )

    body = _draft(client, candidate["id"]).json()

    assert "joining date" not in body["draft"]["body"]
    assert body["guardrails_removed"]


def test_a_draft_that_is_entirely_promises_becomes_the_safe_template(
    client, make_candidate, use_provider
):
    """Nothing usable survives the scrub, so what comes back is no longer the
    model's message — and is not reported as one."""
    candidate = _medium_floor_candidate(make_candidate)
    use_provider(draft_json(body="We can definitely revise your compensation."))

    body = _draft(client, candidate["id"]).json()

    assert body["meta"]["was_fallback"] is True
    assert body["draft"]["body"].strip()
    assert "compensation" not in body["draft"]["body"]


def test_a_whatsapp_draft_with_a_subject_is_repaired(client, make_candidate, use_provider):
    """The subject/channel validator inside the pipeline, not just in
    isolation."""
    candidate = _medium_floor_candidate(make_candidate)
    provider = use_provider(
        draft_json(channel="whatsapp", subject="Checking in"),
        draft_json(channel="whatsapp", subject=None, body="hey Sana, all good on your end?"),
    )

    body = _draft(client, candidate["id"], channel="whatsapp").json()

    assert provider.call_count == 2
    assert body["meta"]["validation_status"] == "repaired"
    assert body["draft"]["subject"] is None


def test_the_draft_prompt_carries_the_requested_intent_and_tone(
    client, make_candidate, use_provider
):
    candidate = _medium_floor_candidate(make_candidate)
    provider = use_provider(draft_json(tone="formal"))

    _draft(client, candidate["id"], intent="ask about relocation support", tone="formal")

    assert "ask about relocation support" in provider.prompts[0]
    assert "TONE: formal" in provider.prompts[0]


def test_draft_message_rejects_an_unsupported_channel(client, make_candidate, use_provider):
    """Request validation, not model validation: a drafted phone call is not a
    thing, even though NextAction can recommend one."""
    candidate = _medium_floor_candidate(make_candidate)
    use_provider(draft_json())

    resp = _draft(client, candidate["id"], channel="call")

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# 7. Summarize and recommend-action
# ---------------------------------------------------------------------------


def test_summarize_persists_without_touching_risk(client, make_candidate, use_provider):
    candidate = _medium_floor_candidate(make_candidate)
    before = _db_candidate(candidate["id"])
    use_provider(summary_json())

    body = client.post(f"{API}/ai/candidates/{candidate['id']}/summarize").json()

    assert body["summary"]["sentiment"] == "concerned"
    assert body["meta"]["analysis_type"] == "interaction_summary"

    rows = _analyses(candidate["id"])
    assert len(rows) == 1
    assert rows[0].risk_level is None, "only a risk assessment sets the risk_level column"

    after = _db_candidate(candidate["id"])
    assert (after.risk_level, after.risk_source) == (before.risk_level, before.risk_source)


def test_recommend_action_persists(client, make_candidate, use_provider):
    candidate = _medium_floor_candidate(make_candidate)
    use_provider(next_action_json())

    body = client.post(f"{API}/ai/candidates/{candidate['id']}/recommend-action").json()

    assert body["recommendation"]["action_type"] == "schedule_call"
    assert body["recommendation"]["suggested_timing_days"] == 0
    assert _analyses(candidate["id"])[0].analysis_type == "next_action"


def test_the_latest_analysis_surfaces_on_the_candidate_detail(
    client, make_candidate, use_provider
):
    """Module 2 already exposes latest_ai_analysis; this is the first module
    that fills it."""
    candidate = _medium_floor_candidate(make_candidate)
    use_provider(risk_json("high"))
    _assess(client, candidate["id"])

    detail = client.get(f"{API}/candidates/{candidate['id']}").json()

    assert detail["latest_ai_analysis"]["analysis_type"] == "risk_assessment"
    assert detail["latest_ai_analysis"]["parsed_output"]["risk_level"] == "high"
    assert detail["latest_ai_analysis"]["was_fallback"] is False


# ---------------------------------------------------------------------------
# 8. HR override endpoint
# ---------------------------------------------------------------------------


def _override(client, candidate_id: str, actor: str = "hr-lead", **payload):
    return client.post(
        f"{API}/ai/candidates/{candidate_id}/override",
        json=payload,
        headers={"X-Actor": actor},
    )


def test_overriding_the_risk_level_sets_hr_override_and_audits_the_ai_level(
    client, make_candidate, use_provider
):
    candidate = _medium_floor_candidate(make_candidate)
    use_provider(risk_json("high"))
    assessment = _assess(client, candidate["id"]).json()

    resp = _override(
        client,
        candidate["id"],
        risk_level="low",
        reason="Spoke to her directly, the meeting was about a farewell, not a counter-offer.",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["candidate"]["risk_level"] == "low"
    assert body["candidate"]["risk_source"] == "hr_override"
    assert body["recorded"]

    with SessionLocal() as db:
        row = db.scalar(
            select(AuditLog).where(
                AuditLog.entity_id == uuid.UUID(candidate["id"]),
                AuditLog.action == "ai_risk_override",
            )
        )
    assert row.actor == "hr-lead"
    assert row.before["risk_level"] == "high"
    assert row.after["risk_level"] == "low"
    assert row.after["overridden_ai_level"] == "high"
    assert row.after["overridden_analysis_id"] == assessment["meta"]["analysis_id"]
    assert "farewell" in row.after["reason"]


def test_an_override_survives_the_next_assessment(client, make_candidate, use_provider):
    candidate = _medium_floor_candidate(make_candidate)
    _override(client, candidate["id"], risk_level="low", reason="Confirmed on a call.")

    use_provider(risk_json("high"))
    body = _assess(client, candidate["id"]).json()

    assert body["risk"]["applied"] is False
    assert _db_candidate(candidate["id"]).risk_level == RiskLevel.LOW


def test_overriding_a_recommendation_is_recorded_against_the_analysis(
    client, make_candidate, use_provider
):
    """It changes no column — and is kept anyway. Nobody labels which
    candidates were about to drop out, so a recruiter marking a
    recommendation wrong is the only correction signal this system will ever
    get."""
    candidate = _medium_floor_candidate(make_candidate)
    use_provider(next_action_json())
    recommendation = client.post(
        f"{API}/ai/candidates/{candidate['id']}/recommend-action"
    ).json()

    resp = _override(
        client,
        candidate["id"],
        recommendation_verdict="rejected",
        reason="A call would spook her this close to joining; email is right.",
    )
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        row = db.scalar(
            select(AuditLog).where(
                AuditLog.entity_id == uuid.UUID(recommendation["meta"]["analysis_id"]),
                AuditLog.action == "ai_recommendation_override",
            )
        )
    assert row.entity_type == "ai_analysis"
    assert row.after["verdict"] == "rejected"
    assert row.before["recommendation"]["action_type"] == "schedule_call"


def test_overriding_a_recommendation_that_does_not_exist_is_a_404(client, make_candidate):
    candidate = _medium_floor_candidate(make_candidate)

    resp = _override(
        client, candidate["id"], recommendation_verdict="accepted", reason="Looks right."
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_an_override_must_name_a_target(client, make_candidate):
    candidate = _medium_floor_candidate(make_candidate)

    resp = _override(client, candidate["id"], reason="I disagree.")

    assert resp.status_code == 422


def test_an_override_must_carry_a_reason(client, make_candidate):
    """The reason is the whole value of the record. An override with no reason
    is an unexplained change to a badge, which is what the audit log exists to
    prevent."""
    candidate = _medium_floor_candidate(make_candidate)

    resp = _override(client, candidate["id"], risk_level="low")

    assert resp.status_code == 422


def test_ai_endpoints_404_on_an_unknown_candidate(client, use_provider):
    use_provider(risk_json())
    missing = uuid.uuid4()

    resp = client.post(f"{API}/ai/candidates/{missing}/assess-risk")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
