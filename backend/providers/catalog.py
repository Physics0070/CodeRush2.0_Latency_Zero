"""Task-aware model routing: which model handles which task, and why.

A model's fit for a task is scored across six axes - reasoning and logic,
task proficiency, context handling, agentic capability, factuality and
safety, and real-world/current data - then weighted by what the *current
question* actually needs (its `Plan.intent`, see backend/chat/planner.py).

The scores below are curated estimates, not live-measured: this repo has no
benchmark-running infrastructure wired to model selection
(backend/benchmarks/{bfcl,swebench,show}.py is a separate, unrelated
SWE-bench/BFCL dataset-fetching block, not a routing input). Treat CATALOG as
an editable, documented opinion, not a claim of measurement.
"""

from pydantic import BaseModel

from backend.providers.registry import parse_model

AXES = (
    "reasoning_logic",
    "task_proficiency",
    "context_relation",
    "agentic_capability",
    "factuality_safety",
    "real_world_data",
)

_LABELS = {
    "reasoning_logic": "reasoning & logic",
    "task_proficiency": "task proficiency",
    "context_relation": "context handling",
    "agentic_capability": "agentic capability",
    "factuality_safety": "factuality & safety",
    "real_world_data": "real-world/current data",
}


class ModelProfile(BaseModel):
    reasoning_logic: float
    task_proficiency: float
    context_relation: float
    agentic_capability: float
    factuality_safety: float
    real_world_data: float


# Keyed by substring against the model *tag* (the part after "provider:" -
# via parse_model, not a naive split, since Ollama tags contain their own
# colons e.g. "llama3.2:3b"). Longest matching key wins, so a specific tag
# (e.g. "llama3.2:3b") outranks a family prefix (e.g. "llama").
CATALOG: dict[str, ModelProfile] = {
    "gpt-oss:120b-cloud": ModelProfile(
        reasoning_logic=0.85, task_proficiency=0.80, context_relation=0.75,
        agentic_capability=0.80, factuality_safety=0.75, real_world_data=0.55,
    ),
    "llama-3.3-70b": ModelProfile(
        reasoning_logic=0.75, task_proficiency=0.75, context_relation=0.70,
        agentic_capability=0.65, factuality_safety=0.65, real_world_data=0.50,
    ),
    "gemini-2.0-flash": ModelProfile(
        reasoning_logic=0.65, task_proficiency=0.70, context_relation=0.80,
        agentic_capability=0.55, factuality_safety=0.70, real_world_data=0.80,
    ),
    "qwen2.5:7b": ModelProfile(
        reasoning_logic=0.60, task_proficiency=0.65, context_relation=0.60,
        agentic_capability=0.50, factuality_safety=0.55, real_world_data=0.35,
    ),
    "llama3.2:3b": ModelProfile(
        reasoning_logic=0.35, task_proficiency=0.45, context_relation=0.45,
        agentic_capability=0.30, factuality_safety=0.40, real_world_data=0.25,
    ),
}

# A neutral, explicitly-labelled fallback for any model tag not in CATALOG,
# so an unrecognised model can still be ranked rather than crashing selection.
DEFAULT_PROFILE = ModelProfile(
    reasoning_logic=0.5, task_proficiency=0.5, context_relation=0.5,
    agentic_capability=0.5, factuality_safety=0.5, real_world_data=0.5,
)

# One weight vector per Plan.intent (backend/chat/planner.py's closed enum).
# Intent-level, not per-specialist free text: it's already computed once per
# turn, and keyword-matching arbitrary role strings would be fragile.
WEIGHTS: dict[str, dict[str, float]] = {
    "chat": {
        "context_relation": 0.30, "task_proficiency": 0.25, "reasoning_logic": 0.15,
        "factuality_safety": 0.15, "real_world_data": 0.10, "agentic_capability": 0.05,
    },
    "explain": {
        "reasoning_logic": 0.25, "task_proficiency": 0.25, "context_relation": 0.25,
        "factuality_safety": 0.15, "agentic_capability": 0.05, "real_world_data": 0.05,
    },
    "analyze": {
        "reasoning_logic": 0.35, "factuality_safety": 0.20, "task_proficiency": 0.20,
        "context_relation": 0.15, "agentic_capability": 0.05, "real_world_data": 0.05,
    },
    "compare": {
        "reasoning_logic": 0.30, "factuality_safety": 0.20, "real_world_data": 0.20,
        "task_proficiency": 0.15, "context_relation": 0.10, "agentic_capability": 0.05,
    },
    "audit": {
        "factuality_safety": 0.35, "reasoning_logic": 0.30, "task_proficiency": 0.15,
        "agentic_capability": 0.10, "context_relation": 0.05, "real_world_data": 0.05,
    },
    "build": {
        "agentic_capability": 0.35, "task_proficiency": 0.30, "reasoning_logic": 0.20,
        "context_relation": 0.10, "factuality_safety": 0.03, "real_world_data": 0.02,
    },
}


def profile_for(model_spec: str) -> tuple[ModelProfile, str]:
    """Returns (profile, matched_key). matched_key is "default" when no
    catalog entry's tag substring matched."""
    try:
        _, model = parse_model(model_spec)
    except ValueError:
        return DEFAULT_PROFILE, "default"
    matches = [k for k in CATALOG if k in model]
    if not matches:
        return DEFAULT_PROFILE, "default"
    key = max(matches, key=len)
    return CATALOG[key], key


def _weights_for(intent: str) -> dict[str, float]:
    return WEIGHTS.get(intent, WEIGHTS["explain"])


def _reason(profile: ModelProfile, weights: dict[str, float], intent: str) -> str:
    top2 = sorted(AXES, key=lambda a: getattr(profile, a) * weights[a], reverse=True)[:2]
    article = "an" if intent[:1] in "aeiou" else "a"
    return f"strongest on {' and '.join(_LABELS[a] for a in top2)} for {article} {intent} task"


def rank_models(models: list[str], intent: str) -> list[tuple[str, str]]:
    """Full ranking, best first, as [(model, reason), ...].

    Pure function of (models, intent) - deterministic, so callers that must
    stay comparable across repeated calls with the same inputs (e.g.
    backend/metrics/marginal_value.py's depth sweep) get the same order every
    time. Ties keep input order (stable sort).
    """
    if not models:
        return []
    weights = _weights_for(intent)
    scored = []
    for m in models:
        profile, _ = profile_for(m)
        score = sum(getattr(profile, a) * weights[a] for a in AXES)
        scored.append((m, score, profile))
    scored.sort(key=lambda t: t[1], reverse=True)
    return [(m, _reason(p, weights, intent)) for m, _, p in scored]


def pick_model(models: list[str], intent: str) -> tuple[str, str]:
    """Best single pick, (model, reason). Raises IndexError on an empty list -
    callers already guard against no models being configured upstream."""
    return rank_models(models, intent)[0]
