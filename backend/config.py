"""Application settings.

Single place env vars are read - with one deliberate exception: provider API keys
are NOT declared here. They are read exclusively by
`backend/providers/secret_broker.py` (block 2), so grepping for a key name
returns exactly one module.
"""

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---------- application ----------
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Render sets $PORT itself; 7860 is only the local-dev fallback.
    port: int = 7860

    # ---------- persistence ----------
    turso_database_url: str = ""
    turso_auth_token: str = ""

    # ---------- execution contract ----------
    max_repair_attempts: int = Field(default=2, ge=0, le=10)
    default_seed: int = 42
    default_temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    # ---------- engine budgets ----------
    default_budget_tokens: int = Field(default=8000, gt=0)
    default_timeout_s: int = Field(default=60, gt=0)

    # ---------- security ----------
    # No default. An unset SECRET_KEY is blank in dev and fatal in prod (below),
    # so a shipped fallback secret can never silently become the live one.
    secret_key: str = ""
    cors_origins: str = "http://localhost:5173"
    # Single Netlify (or other) frontend origin, added to cors_origin_list.
    # Blank in dev on purpose - see _require_secrets_in_prod.
    frontend_origin: str = ""
    max_upload_bytes: int = Field(default=5 * 1024 * 1024, gt=0)

    # ---------- LLM provider ----------
    # Which provider backend.providers._default_models() builds specs for.
    # Only "ollama" is implemented; the field is a plain str (not a Literal) so
    # a second provider is a registry entry away, not a schema change. Blank in
    # dev (falls back to "ollama" at the point of use); required in prod so a
    # forgotten env var fails at boot, not on the first chat request.
    llm_provider: str = ""
    # Ollama Cloud or a local/tunnelled instance - same adapter either way.
    # Blank default is deliberate: defaulting this to localhost in prod would
    # have the backend silently try to reach itself instead of failing fast.
    # The adapter falls back to localhost only outside prod (see secret_broker).
    ollama_base_url: str = ""
    # Comma-separated model tags, no "ollama:" prefix (e.g. "llama3.2:3b" for a
    # local/dev instance, "qwen3-coder:480b-cloud" for Ollama Cloud - a local
    # tag and a :cloud tag are interchangeable here, no code change needed).
    ollama_models: str = "llama3.2:3b,qwen2.5:7b"
    # Cap on simultaneous in-flight model calls across the whole council, so a
    # wide fanout cannot blow through the provider's rate limit at once.
    max_concurrent_model_calls: int = Field(default=4, gt=0)

    # ---------- rate limiting: thresholds are configuration, never constants ----------
    rate_limit_auth: str = "5/minute"
    rate_limit_public: str = "30/minute"
    rate_limit_authed: str = "120/minute"

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, data: Any) -> Any:
        """`cp .env.example .env` leaves every value blank.

        pydantic reads a blank line as "" rather than unset, which fails eight
        typed fields before the app can start. Drop blanks so field defaults
        apply and a fresh clone boots.
        """
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v != ""}
        return data

    @field_validator("cors_origins")
    @classmethod
    def _reject_wildcard(cls, v: str) -> str:
        if v.strip() == "*":
            raise ValueError("CORS_ORIGINS must be an explicit allow-list, not '*'")
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @model_validator(mode="after")
    def _require_secrets_in_prod(self) -> "Settings":
        if self.is_prod:
            missing = [
                name
                for name in (
                    "secret_key", "turso_database_url", "turso_auth_token",
                    "llm_provider", "ollama_base_url", "frontend_origin",
                )
                if not getattr(self, name)
            ]
            # OLLAMA_API_KEY is deliberately not required here: an
            # unauthenticated local/tunnelled Ollama instance is a valid prod
            # configuration too, not just a dev convenience.
            if missing:
                names = ", ".join(n.upper() for n in missing)
                raise ValueError(f"missing required prod settings: {names}")
        return self


settings = Settings()
