"""
workflow_dna/ml/train.py

Phase 3: trains the RandomForestRegressor that predicts a workflow's
Fitness Score from its 9 canonical DNA features, using the training
table produced by run_preprocessing.py (Phase 2).

What "training" means here, in plain terms: we show the model 1,262
already-completed workflow executions, each with its 9
structural/behavioral features AND the fitness score it actually
achieved. The model looks for patterns — "workflows shaped like THIS
tend to score HIGH, workflows shaped like THAT tend to score LOW" — and
encodes those patterns as a forest of decision trees. Once trained, it
can look at a BRAND NEW workflow's features (before it's ever executed)
and predict roughly what fitness score it would achieve, based on how
similar past workflows performed.

We hold back 20% of the data (the "test set") that the model never
sees during training, purely to check afterward whether its predictions
generalize to workflows it hasn't memorized, rather than just
memorizing the training rows.

Run directly with: python -m workflow_dna.ml.train
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import cross_val_score, train_test_split

from backend.workflow_dna.ml.feature_engineering import CANONICAL_FEATURE_COLUMNS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRAINING_TABLE_PATH = PROJECT_ROOT / "dataset" / "processed" / "workflow_dna_training_features.csv"
ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"
MODEL_PATH = ARTIFACTS_DIR / "workflow_fitness_model.joblib"


def load_training_table(path: str) -> pd.DataFrame:
    """Loads Phase 2's output table and validates it has everything training needs."""
    df = pd.read_csv(path)
    missing = [c for c in CANONICAL_FEATURE_COLUMNS + ["fitness"] if c not in df.columns]
    if missing:
        raise ValueError(f"Training table missing expected columns: {missing}")
    return df


def check_feature_importance_candidates(X: pd.DataFrame, y: pd.Series) -> pd.Series:
    """
    Quick diagnostic fit (NOT the final saved model) purely to display
    which features the model actually leans on before committing to the
    real training run. This is the project's required "validate every
    feature / display importance candidates / decide whether to remove
    any" step, re-run here on the final 9-feature schema against the
    engineered fitness target (Phase 1's version of this check used the
    raw columns against the raw, since-discarded Fitness column).
    """
    probe = RandomForestRegressor(n_estimators=200, random_state=42, n_jobs=-1)
    probe.fit(X, y)
    return pd.Series(probe.feature_importances_, index=X.columns).sort_values(ascending=False)


def train_model(df: pd.DataFrame):
    X = df[CANONICAL_FEATURE_COLUMNS]
    y = df["fitness"]

    print("=== Feature importance candidates (diagnostic pre-check) ===")
    importances = check_feature_importance_candidates(X, y)
    print(importances)

    # Decision: keep all 9 features even though parallel_enabled and
    # mutation_type_encoded currently carry almost no signal (documented
    # in Phase 2 — this pretraining dataset has near-zero variance in
    # both columns). Dropping them now would mean re-adding them later
    # once live orchestrator data gives them real variance, and
    # RandomForest tolerates low-signal features without meaningfully
    # hurting accuracy. So: keep the full 9-feature schema for forward
    # compatibility with live data, don't drop anything at this stage.

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42
    )

    model = RandomForestRegressor(
        n_estimators=300,
        max_depth=None,
        min_samples_leaf=2,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    metrics = {
        "r2": round(r2_score(y_test, y_pred), 4),
        "mae": round(mean_absolute_error(y_test, y_pred), 4),
        "rmse": round(mean_squared_error(y_test, y_pred) ** 0.5, 4),
        "train_rows": int(len(X_train)),
        "test_rows": int(len(X_test)),
    }

    # 5-fold cross-validation gives a more robust accuracy estimate than
    # a single train/test split, since it rotates which 20% is "held out"
    # five times and averages the result.
    cv_scores = cross_val_score(model, X, y, cv=5, scoring="r2")
    metrics["cv_r2_mean"] = round(cv_scores.mean(), 4)
    metrics["cv_r2_std"] = round(cv_scores.std(), 4)

    final_importances = pd.Series(
        model.feature_importances_, index=X.columns
    ).sort_values(ascending=False)

    return model, metrics, final_importances


def main():
    df = load_training_table(str(TRAINING_TABLE_PATH))
    model, metrics, importances = train_model(df)

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)

    (ARTIFACTS_DIR / "model_metrics.json").write_text(json.dumps(metrics, indent=2))
    (ARTIFACTS_DIR / "feature_importance.json").write_text(
        json.dumps(importances.round(4).to_dict(), indent=2)
    )

    print("\n=== Final model evaluation (held-out test set + 5-fold CV) ===")
    print(json.dumps(metrics, indent=2))
    print("\n=== Final feature importances ===")
    print(importances)
    print(f"\nModel saved to {MODEL_PATH}")


if __name__ == "__main__":
    main()
