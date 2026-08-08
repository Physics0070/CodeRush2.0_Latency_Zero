"""
workflow_dna/ml/model_service.py

A small, dependency-light service that loads the trained model once and
exposes a single clean method — predict_fitness() — for everything
downstream (Phase 4's live prediction service, the mutation engine,
etc.) to call. Callers don't need to know joblib, sklearn, or artifact
file paths — just a dict of the 9 canonical features in, a fitness
number out.

This is the ONLY file other phases should import from to get
predictions. Everything else in ml/ (preprocessing, feature_engineering,
train) is implementation detail behind this interface.
"""

from pathlib import Path

import joblib
import pandas as pd

from backend.workflow_dna.ml.feature_engineering import CANONICAL_FEATURE_COLUMNS

ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"
DEFAULT_MODEL_PATH = ARTIFACTS_DIR / "workflow_fitness_model.joblib"


class FitnessModelService:
    """
    Loads the trained RandomForestRegressor once and reuses it for every
    prediction call, avoiding the cost of reloading from disk each time.

    Usage:
        service = FitnessModelService()
        predicted_fitness = service.predict_fitness({
            "workflow_length": 40,
            "number_of_agents": 5,
            "parallel_enabled": 1,
            "retry_count": 0,
            "execution_time": 30.0,
            "cost": 0.02,
            "success": 1.0,
            "workflow_depth": 12,
            "mutation_type_encoded": 0,
        })

    NOTE (heads-up for Phase 4): this model was trained on COMPLETE
    historical records, where every one of the 9 features is known
    because the workflow already finished executing. `success`,
    `execution_time`, `cost`, and `retry_count` are only knowable
    AFTER a workflow runs — so calling predict_fitness() on a
    not-yet-executed workflow means those 4 fields have to be
    estimated (e.g. from historical averages of similarly-shaped past
    workflows) rather than measured. That estimation logic belongs in
    Phase 4's prediction service, not here — this class only wraps the
    trained model itself.
    """

    def __init__(self, model_path: str | None = None):
        path = Path(model_path) if model_path else DEFAULT_MODEL_PATH
        if not path.exists():
            raise FileNotFoundError(
                f"No trained model found at {path}. Run `python -m workflow_dna.ml.train` first."
            )
        self.model = joblib.load(path)

    def predict_fitness(self, features: dict) -> float:
        """
        Predicts a fitness score for a single workflow's feature dict.
        Keys must match CANONICAL_FEATURE_COLUMNS exactly — missing
        keys raise a clear error rather than silently defaulting to 0,
        since a silent default could quietly produce a misleading score.
        """
        missing = [c for c in CANONICAL_FEATURE_COLUMNS if c not in features]
        if missing:
            raise ValueError(f"Missing required feature(s) for prediction: {missing}")

        row = pd.DataFrame([{c: features[c] for c in CANONICAL_FEATURE_COLUMNS}])
        prediction = self.model.predict(row)[0]
        return round(float(prediction), 2)

    def predict_batch(self, feature_rows: list) -> list:
        """
        Same as predict_fitness but for multiple workflows at once —
        e.g. scoring an original workflow against its mutated variant
        in the same call, which Phase 4's mutation engine will need.
        """
        return [self.predict_fitness(row) for row in feature_rows]
