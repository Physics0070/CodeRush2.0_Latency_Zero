"""Block 0 smoke test: the app boots, /health answers 200, prod refuses to run blind."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.config import Settings
from backend.main import app

client = TestClient(app)


def test_health_returns_200() -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_leaks_no_configuration() -> None:
    """A liveness probe must not become an information-disclosure endpoint."""
    assert set(client.get("/health").json()) == {"status", "version"}


def test_prod_refuses_to_boot_without_secrets() -> None:
    """Prod must fail loudly at startup, not fall back to a shipped default."""
    with pytest.raises(ValidationError, match="missing required prod settings"):
        Settings(app_env="prod", secret_key="", supabase_url="", supabase_service_key="")


def test_cors_wildcard_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(cors_origins="*")
