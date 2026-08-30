"""Shared constants and plain helper functions for the API tests.

Fixtures live in conftest.py; anything importable (constants, the fake LLM
provider, payload builders, the teardown purge) lives here so test modules can
import it directly rather than reaching into conftest or into each other —
pytest fixtures do not cross test modules, and importing one test module from
another to borrow a helper is how a suite becomes impossible to reorder.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

from sqlalchemy import delete, func, select

from app.ai.provider import LLMProvider
from app.db import SessionLocal
from app.models import (
    AIAnalysis,
    AuditLog,
    Candidate,
    CandidateStage,
    FollowUpAction,
    Interaction,
)

API = "/api/v1"

# Tables whose row counts must be identical before and after the whole run.
GUARDED_MODELS = (Candidate, CandidateStage, Interaction, AuditLog, FollowUpAction, AIAnalysis)


def row_counts() -> dict[str, int]:
    with SessionLocal() as db:
        return {
            model.__tablename__: db.scalar(select(func.count()).select_from(model)) or 0
            for model in GUARDED_MODELS
        }


def purge_candidates(candidate_ids: list[str]) -> None:
    """Delete test-created candidates and everything hanging off them.

    audit_log has no foreign key, so its rows are matched on entity_id across
    every entity that hangs off a candidate: the candidate itself, its
    candidate_stages rows, its ai_analyses rows (an AI recommendation override
    is audited against the analysis it disagreed with) and its
    follow_up_actions rows (automation audits the action it created).
    """
    if not candidate_ids:
        return

    ids = [uuid.UUID(cid) for cid in candidate_ids]
    with SessionLocal() as db:
        stage_ids = list(
            db.scalars(select(CandidateStage.id).where(CandidateStage.candidate_id.in_(ids)))
        )
        analysis_ids = list(
            db.scalars(select(AIAnalysis.id).where(AIAnalysis.candidate_id.in_(ids)))
        )
        action_ids = list(
            db.scalars(select(FollowUpAction.id).where(FollowUpAction.candidate_id.in_(ids)))
        )
        db.execute(
            delete(AuditLog).where(
                AuditLog.entity_id.in_(ids + stage_ids + analysis_ids + action_ids)
            )
        )
        db.execute(delete(Interaction).where(Interaction.candidate_id.in_(ids)))
        db.execute(delete(CandidateStage).where(CandidateStage.candidate_id.in_(ids)))
        db.execute(delete(AIAnalysis).where(AIAnalysis.candidate_id.in_(ids)))
        db.execute(delete(FollowUpAction).where(FollowUpAction.candidate_id.in_(ids)))
        db.execute(delete(Candidate).where(Candidate.id.in_(ids)))
        db.commit()


def unique_email(prefix: str = "test") -> str:
    return f"{prefix}.{uuid.uuid4().hex[:10]}@example.com"


def candidate_payload(default_recruiter_id: str, **overrides) -> dict:
    """Build a valid POST /candidates body.

    The positional argument is named `default_recruiter_id` rather than
    `recruiter_id` so that `recruiter_id=...` can be passed as an override —
    with the obvious name it collides with the positional parameter and raises
    TypeError before the function body runs, which makes it impossible to
    create a candidate under a specific recruiter.
    """
    offer_date = date.today() - timedelta(days=10)
    payload = {
        "name": "Test Candidate",
        "email": unique_email(),
        "role": "Software Engineer II",
        "department": "Engineering",
        "location": "Bengaluru",
        "offer_date": offer_date.isoformat(),
        "joining_date": (offer_date + timedelta(days=60)).isoformat(),
        "recruiter_id": default_recruiter_id,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Fake LLM provider
#
# Used by every test that exercises the AI layer or the automation sweep. No
# test in this suite calls the real Gemini API.
# ---------------------------------------------------------------------------


class ProviderCalledTooOften(BaseException):
    """Deliberately a BaseException, not an Exception.

    The engine catches Exception around every provider call and degrades to
    its fallback — correct behaviour, and it would silently swallow this and
    hide the exact thing the repair and idempotency tests exist to assert. A
    BaseException escapes the engine and fails the test loudly.
    """


class FakeProvider(LLMProvider):
    """Returns queued responses in order. An `Exception` in the queue is
    raised instead of returned, so provider failures are expressed the same
    way as provider successes.

    Running past the end of the queue is an error, not a repeat: "exactly one
    repair attempt" and "a repeat sweep makes no AI calls" are only real
    assertions if an extra call fails.
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


# ---------------------------------------------------------------------------
# Contract payload builders — valid by default, overridable per field so a
# test can make exactly one thing wrong.
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
