"""Block 3 verification - the graded core.

Two scripted-producer cases prove the loop deterministically, then one case
drives a real local model so the repair path is proven against an actual LLM
rather than only against a fixture.
"""

import os
import uuid

import httpx
import pytest

from backend.config import settings
from backend.events import EventStore, EventType
from backend.handoff import artifact_store, repair_loop, validate

pytestmark = pytest.mark.skipif(
    not (settings.turso_database_url and settings.turso_auth_token),
    reason="turso not configured",
)


def _ollama_reachable() -> bool:
    """Ollama is dev-only now, not the default path (see backend/api/routes.py
    _default_models), so a test that needs a live one must check rather than
    assume - CI and a from-scratch clone have no reason to have it running."""
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    try:
        httpx.get(f"{base}/api/tags", timeout=2.0)
        return True
    except httpx.HTTPError:
        return False

FINDING_SCHEMA = {
    "type": "object",
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["title", "severity"],
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "severity": {"enum": ["low", "medium", "high", "critical"]},
                },
            },
        }
    },
}


@pytest.fixture
async def store():
    async with EventStore() as s:
        yield s


@pytest.fixture
async def run_id(store):
    rid = str(uuid.uuid4())
    await store.create_run(rid, "handoff test", "h", 42, {}, "running")
    return rid


# ---------------------------------------------------------------- validation


def test_validate_collects_every_error_not_just_the_first():
    r = validate({"findings": [{"severity": "catastrophic"}]}, FINDING_SCHEMA)
    assert not r.ok
    assert len(r.errors) == 2, r.errors  # missing title AND bad severity enum


def test_validate_strips_markdown_fence():
    r = validate('```json\n{"findings":[{"title":"x","severity":"low"}]}\n```', FINDING_SCHEMA)
    assert r.ok


def test_validate_rejects_prose():
    r = validate("the repository seems fine overall", FINDING_SCHEMA)
    assert not r.ok
    assert "not valid JSON" in r.errors[0] or "empty" in r.errors[0]


# ---------------------------------------------------------------- repair loop


async def test_malformed_is_rejected_then_repaired_on_retry_one(store, run_id):
    attempts = []

    async def produce(feedback):
        attempts.append(feedback)
        if feedback is None:
            return '{"findings": [{"severity": "high"}]}'  # missing `title`
        assert "title" in feedback, "the exact validator error must reach the producer"
        return '{"findings": [{"title": "Hardcoded secret", "severity": "high"}]}'

    out = await repair_loop(
        store, run_id, produce=produce, schema=FINDING_SCHEMA, agent_id="security", node_id="n1"
    )

    assert out.ok
    assert out.attempts == 1
    assert out.payload["findings"][0]["title"] == "Hardcoded secret"

    kinds = [e.event_type for e in await store.read(run_id)]
    assert kinds == [
        EventType.HANDOFF_EMITTED,
        EventType.HANDOFF_REJECTED,
        EventType.REPAIR_ATTEMPT,
        EventType.HANDOFF_EMITTED,
        EventType.HANDOFF_VALIDATED,
    ]


async def test_unrepairable_exhausts_budget_and_fails_the_branch(store, run_id):
    async def produce(_):
        return "still not json"

    out = await repair_loop(
        store, run_id, produce=produce, schema=FINDING_SCHEMA,
        agent_id="broken", node_id="n2", max_attempts=2,
    )

    assert not out.ok
    assert out.attempts == 3  # 1 emission + 2 repairs

    kinds = [e.event_type for e in await store.read(run_id)]
    assert kinds[-1] == EventType.BRANCH_FAILED
    assert kinds.count(EventType.REPAIR_ATTEMPT) == 2
    assert kinds.count(EventType.HANDOFF_REJECTED) == 3


async def test_artifact_carries_provenance(store, run_id):
    async def produce(_):
        return '{"findings":[{"title":"Missing rate limit","severity":"medium"}]}'

    out = await repair_loop(
        store, run_id, produce=produce, schema=FINDING_SCHEMA, agent_id="quality", node_id="n3"
    )
    art = await artifact_store.put(
        store, run_id, seq=out.final_seq, written_by="quality",
        content=out.payload, node_id="n3",
    )
    assert art.written_by == "quality"
    assert art.run_id == run_id
    assert art.seq == out.final_seq
    assert art.ts is not None


# ------------------------------------------------- real model, not a fixture


@pytest.mark.skipif(not _ollama_reachable(), reason="ollama not running locally")
async def test_repair_loop_against_a_real_local_model(store, run_id):
    """Same loop, driven by ollama. Proves the contract holds against an LLM."""
    from backend.providers import complete

    system = "You output JSON only."
    goal = (
        "List exactly one security finding for a web app as JSON matching: "
        '{"findings":[{"title":<string>,"severity":"low"|"medium"|"high"|"critical"}]}'
    )

    async def produce(feedback):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": goal}]
        if feedback:
            messages.append({"role": "user", "content": feedback})
        c = await complete("ollama:llama3.2:3b", messages, seed=42, max_tokens=200)
        return c.text

    out = await repair_loop(
        store, run_id, produce=produce, schema=FINDING_SCHEMA,
        agent_id="live_security", node_id="live",
    )
    assert out.ok, f"model could not satisfy schema in {out.attempts} attempts: {out.errors}"
    assert len(out.payload["findings"]) >= 1
