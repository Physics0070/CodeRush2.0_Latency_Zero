"""Task-aware model routing. Pure - no network, no model calls."""

from backend.providers.catalog import AXES, WEIGHTS, pick_model, profile_for, rank_models


def test_every_weight_vector_covers_every_axis_and_sums_to_one():
    for intent, weights in WEIGHTS.items():
        assert set(weights) == set(AXES), f"{intent} weight vector is missing an axis"
        assert abs(sum(weights.values()) - 1.0) < 1e-9, f"{intent} weights do not sum to 1"


def test_unknown_model_tag_gets_the_documented_neutral_default():
    profile, matched = profile_for("openrouter:some-brand-new-model-9000")
    assert matched == "default"
    assert all(getattr(profile, a) == 0.5 for a in AXES)


def test_known_model_tag_matches_by_longest_substring():
    _, matched = profile_for("ollama:gpt-oss:120b-cloud")
    assert matched == "gpt-oss:120b-cloud"


def test_rank_models_is_deterministic():
    models = ["ollama:gpt-oss:120b-cloud", "groq:llama-3.3-70b-versatile",
              "gemini:gemini-2.0-flash"]
    assert rank_models(models, "audit") == rank_models(models, "audit")


def test_rank_models_orders_best_first():
    models = ["ollama:llama3.2:3b", "ollama:gpt-oss:120b-cloud"]
    ranked = rank_models(models, "audit")
    assert [m for m, _ in ranked] == ["ollama:gpt-oss:120b-cloud", "ollama:llama3.2:3b"]


def test_pick_model_returns_a_model_and_a_nonempty_reason():
    model, reason = pick_model(["ollama:gpt-oss:120b-cloud"], "build")
    assert model == "ollama:gpt-oss:120b-cloud"
    assert reason and "build" in reason


def test_unrecognised_intent_falls_back_to_explain_weights():
    """Same weights (and so the same ranking) as "explain" - the reason text
    still names the intent that was actually asked for, so only the model
    ordering is compared here, not the reason strings."""
    models = ["ollama:gpt-oss:120b-cloud", "ollama:llama3.2:3b"]
    unknown = [m for m, _ in rank_models(models, "not-a-real-intent")]
    explain = [m for m, _ in rank_models(models, "explain")]
    assert unknown == explain


def test_rank_models_of_empty_list_is_empty():
    assert rank_models([], "audit") == []
