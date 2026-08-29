from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    app_env: str = "development"

    # --- LLM (Module 5) -----------------------------------------------------
    # llm_provider selects the implementation in app/ai/provider.py. The
    # gemini_* settings are read only by GeminiProvider; a second provider
    # brings its own.
    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3-flash-preview"

    # Output cap, per CLAUDE.md's guardrails. Generous enough for the longest
    # contract (a drafted email plus its personalization list) with room for
    # the model's reasoning tokens, tight enough that a runaway generation
    # cannot cost unbounded tokens.
    llm_max_output_tokens: int = 2048
    llm_temperature: float = 0.2

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
