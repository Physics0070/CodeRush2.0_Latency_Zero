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
    port: int = 7860  # HF Spaces hardcodes this. Do not change.

    # ---------- persistence ----------
    supabase_url: str = ""
    supabase_service_key: str = ""

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
    max_upload_bytes: int = Field(default=5 * 1024 * 1024, gt=0)

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
                for name in ("secret_key", "supabase_url", "supabase_service_key")
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError(f"missing required prod settings: {', '.join(missing)}")
        return self


settings = Settings()
