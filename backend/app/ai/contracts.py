"""The four AI output contracts, exactly the shapes CLAUDE.md specifies.

These are the boundary between "a language model produced some text" and
"the application has a value it can act on". Every LLM response is parsed
through one of them; nothing else in the app ever touches a raw model
response. A response that does not fit gets exactly one repair attempt and
then a deterministic fallback (see engine.py) — the contract failing is a
normal, handled outcome, not an error.

`json_schema_for()` at the bottom turns a contract into the provider-neutral
JSON schema handed to the model's structured-output mode, so the shape the
model is asked for and the shape we validate against can never drift apart.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# WhatsApp is a chat window, not an inbox. A model left unchecked will happily
# write six paragraphs; anything past roughly this length reads as a form
# letter, which is precisely the "obviously automated nudge" CLAUDE.md warns
# makes a wavering candidate worse.
WHATSAPP_BODY_MAX_CHARS = 700


class RiskAssessment(BaseModel):
    risk_level: Literal["low", "medium", "high"]
    confidence: float = Field(ge=0, le=1)
    signals: list[str] = Field(max_length=5)  # evidence quoted from interactions
    reasoning: str = Field(max_length=500)
    concern_category: Literal[
        "relocation",
        "notice_period",
        "counter_offer",
        "compensation",
        "role_scope",
        "personal",
        "none",
    ]


class NextAction(BaseModel):
    action_type: Literal[
        "send_message",
        "schedule_call",
        "escalate_to_manager",
        "send_documents",
        "no_action_needed",
    ]
    channel: Literal["email", "whatsapp", "call"]
    urgency: Literal["low", "medium", "high"]
    rationale: str
    suggested_timing_days: int = Field(ge=0, le=30)


class DraftedMessage(BaseModel):
    channel: Literal["email", "whatsapp"]
    subject: str | None
    body: str
    tone: Literal["warm", "formal", "casual"]
    personalization_used: list[str]

    @field_validator("subject", mode="before")
    @classmethod
    def _blank_subject_is_no_subject(cls, value: Any) -> Any:
        """Treat "" and "   " as absent.

        Structured-output modes frequently emit an empty string rather than a
        JSON null for a field they have nothing to put in. Failing validation
        over that would burn the single repair attempt on a difference with no
        meaning, so it is normalised here and the real rule is enforced below.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _subject_matches_channel(self) -> "DraftedMessage":
        if self.channel == "email" and self.subject is None:
            raise ValueError("subject is required when channel is 'email'")

        if self.channel == "whatsapp":
            if self.subject is not None:
                raise ValueError("subject must be null when channel is 'whatsapp'")
            if len(self.body) > WHATSAPP_BODY_MAX_CHARS:
                raise ValueError(
                    f"whatsapp body must be at most {WHATSAPP_BODY_MAX_CHARS} characters, "
                    f"got {len(self.body)}"
                )
        return self


class InteractionSummary(BaseModel):
    summary: str = Field(max_length=800)
    key_concerns: list[str]
    sentiment: Literal["positive", "neutral", "concerned", "negative"]
    unresolved_items: list[str]


# ---------------------------------------------------------------------------
# Schema generation for the provider's structured-output mode
# ---------------------------------------------------------------------------

# Keys a provider's structured-output schema understands. Pydantic emits a few
# extras (title, default) that some providers reject outright, so the schema is
# filtered down to this set rather than passed through raw.
_ALLOWED_SCHEMA_KEYS = {
    "type",
    "enum",
    "items",
    "properties",
    "required",
    "nullable",
    "description",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "propertyOrdering",
}


def json_schema_for(contract: type[BaseModel]) -> dict[str, Any]:
    """Return the JSON schema for a contract, in the OpenAPI 3.0 subset that
    structured-output modes accept.

    Derived from the model rather than hand-written, so adding a field to a
    contract cannot leave the model being asked for the old shape.
    """
    schema = _simplify(contract.model_json_schema())
    # Pin the emission order to the declaration order. Without it the provider
    # is free to order keys as it likes, which makes two responses to the same
    # prompt diff against each other for no reason and makes the raw_response
    # column harder to eyeball.
    if "properties" in schema:
        schema["propertyOrdering"] = list(schema["properties"].keys())
    return schema


def _simplify(node: Any) -> Any:
    if isinstance(node, list):
        return [_simplify(item) for item in node]
    if not isinstance(node, dict):
        return node

    # `str | None` arrives as anyOf[{type: string}, {type: null}]. The null
    # branch is not a type these schemas have; it is the `nullable` flag.
    if "anyOf" in node:
        variants = [v for v in node["anyOf"] if v.get("type") != "null"]
        nullable = len(variants) != len(node["anyOf"])
        merged = _simplify(variants[0]) if len(variants) == 1 else {"type": "string"}
        if nullable:
            merged["nullable"] = True
        return merged

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties":
            # A name -> schema map. Its keys are field names, not schema
            # keywords, so they must survive the filter that applies to
            # everything else.
            out[key] = {name: _simplify(sub) for name, sub in value.items()}
        elif key in _ALLOWED_SCHEMA_KEYS:
            out[key] = _simplify(value)
    return out
