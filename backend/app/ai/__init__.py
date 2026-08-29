"""Module 5 — the AI service.

Layout:

    contracts.py   the four Pydantic output shapes the LLM must produce
    prompts.py     versioned prompt templates + candidate context builder
    provider.py    LLMProvider interface and the Gemini implementation
    guardrails.py  post-generation scrubbing of candidate-facing text
    engine.py      the pipeline: prompt -> provider -> validate -> repair ->
                   fallback -> persist

Nothing in here mutates a candidate. Applying an AI risk assessment to the
candidate record (final = max(rule_floor, ai)) lives in
services/ai_service.py, so the rule that the AI may only ever RAISE risk is
enforced in one place next to the rest of the risk logic.
"""
