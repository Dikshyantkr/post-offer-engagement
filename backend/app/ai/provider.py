"""The LLM provider interface and its Gemini implementation.

engine.py knows only `LLMProvider`. Everything Gemini-specific — the client,
the structured-output config, the thinking budget, the exception surface —
lives below and never leaks upward, so swapping in Groq means adding a class
and one line to `_REGISTRY`, selected by the LLM_PROVIDER env var. Nothing in
engine.py changes.

Two deliberate choices about failure:

* `generate_json` raises `LLMProviderError` for every failure mode — network,
  auth, quota, safety block, empty response. The engine catches one exception
  type and falls back; it never has to know what a `google.genai` error looks
  like.
* The client is built lazily on first call, not at import. A missing API key
  or a broken SDK install therefore degrades to the rule-based fallback at
  request time instead of preventing the whole app from starting, which is
  what "the app must never break because the LLM misbehaved" has to mean in
  practice.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from app.config import settings
from app.errors import ConfigurationError, LLMProviderError

logger = logging.getLogger(__name__)


class LLMProvider(ABC):
    """One method: give me JSON matching this schema.

    `model_name` is a plain attribute rather than an abstract property because
    it must be readable even when the provider cannot be reached — every
    ai_analyses row records which model was asked, including the rows written
    after a call failed and the deterministic fallback ran.
    """

    name: str
    model_name: str

    @abstractmethod
    def generate_json(self, prompt: str, schema_hint: dict[str, Any]) -> tuple[str, int]:
        """Return (raw_response, latency_ms).

        The raw response is unparsed text, exactly as the model produced it —
        parsing and validation belong to the engine. Implementations must
        raise LLMProviderError, and only LLMProviderError, on failure.
        """


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str,
        max_output_tokens: int,
        temperature: float,
    ) -> None:
        self.model_name = model_name
        self._api_key = api_key
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        if not self._api_key:
            raise LLMProviderError(
                "GEMINI_API_KEY is not set, so no AI provider is reachable.",
                details={"provider": self.name},
            )

        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise LLMProviderError(
                "The google-genai package is not installed.", details={"provider": self.name}
            ) from exc

        self._client = genai.Client(api_key=self._api_key)
        return self._client

    def generate_json(self, prompt: str, schema_hint: dict[str, Any]) -> tuple[str, int]:
        from google.genai import types

        started = time.perf_counter()
        try:
            response = self._get_client().models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    # Structured-output mode: the schema is enforced by the
                    # provider, so the vast majority of responses parse first
                    # time and the repair path stays the exception it is meant
                    # to be rather than the normal case.
                    response_mime_type="application/json",
                    response_schema=schema_hint,
                    # Capped so a runaway generation cannot cost unbounded
                    # tokens or hold a request open indefinitely.
                    max_output_tokens=self._max_output_tokens,
                    # Low but not zero. This is an analysis task where the same
                    # candidate should score the same way twice; a little
                    # variance keeps drafted messages from reading identically
                    # across candidates.
                    temperature=self._temperature,
                    # Gemini 3 reasons before answering by default, and those
                    # tokens come out of max_output_tokens — leaving it
                    # unbounded is how a generous-looking cap still returns an
                    # empty response.
                    thinking_config=types.ThinkingConfig(thinking_level="low"),
                ),
            )
        except LLMProviderError:
            raise
        except Exception as exc:
            # Deliberately broad: google-genai raises a wide family of errors
            # (transport, auth, quota, serialisation) and the engine's contract
            # is that every one of them arrives as LLMProviderError. The
            # original is chained for the log, never for the client.
            raise LLMProviderError(
                f"Gemini request failed: {type(exc).__name__}",
                details={"provider": self.name, "model": self.model_name},
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        text = (response.text or "").strip() if hasattr(response, "text") else ""
        if not text:
            # A safety block or a generation that spent its whole budget
            # thinking both land here.
            raise LLMProviderError(
                "Gemini returned an empty response.",
                details={"provider": self.name, "model": self.model_name},
            )

        return text, latency_ms


def _build_gemini() -> LLMProvider:
    return GeminiProvider(
        api_key=settings.gemini_api_key,
        model_name=settings.gemini_model,
        max_output_tokens=settings.llm_max_output_tokens,
        temperature=settings.llm_temperature,
    )


# Add a provider here and it is selectable by LLM_PROVIDER. Nothing else moves.
_REGISTRY: dict[str, Any] = {
    "gemini": _build_gemini,
}

_cached: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """The configured provider, built once per process.

    Raises ConfigurationError for an unknown LLM_PROVIDER value. That is a
    deployment mistake, not the LLM misbehaving, and it should be loud — an
    app quietly serving rule-based fallbacks because of a typo in an env var
    is far worse than one that says so.
    """
    global _cached
    if _cached is not None:
        return _cached

    key = settings.llm_provider.strip().lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise ConfigurationError(
            f"Unknown LLM_PROVIDER '{settings.llm_provider}'.",
            details={"supported": sorted(_REGISTRY)},
        )

    _cached = factory()
    logger.info("LLM provider ready: %s (%s)", _cached.name, _cached.model_name)
    return _cached


def reset_provider_cache() -> None:
    """Drop the cached provider. For tests and for reconfiguration."""
    global _cached
    _cached = None
