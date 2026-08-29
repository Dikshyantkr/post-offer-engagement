from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    app_env: str = "development"
    log_level: str = "INFO"

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

    # --- Automation (Module 6) ---------------------------------------------
    # Defaults to true so `docker compose up` schedules the nightly sweep.
    # Tests and CI set it false: a background thread firing real sweeps would
    # create follow-up actions nobody asked for and spend provider quota.
    run_scheduler: bool = True
    sweep_hour: int = 2
    sweep_minute: int = 0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
