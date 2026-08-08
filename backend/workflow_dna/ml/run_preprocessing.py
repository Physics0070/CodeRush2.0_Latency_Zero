"""
workflow_dna/ml/run_preprocessing.py

End-to-end Phase 2 pipeline:

  1. Load + clean the raw dataset          (preprocessing.py)
  2. Engineer the 9 canonical DNA features  (feature_engineering.py)
  3. Fit fitness-normalization stats on this batch, then compute the
     engineered Fitness target (Option B) for every row using the
     shared FitnessCalculator, so this pretrained target is on the
     exact same 0-100 scale the live orchestrator will use later.
  4. Write the final training table + fitted stats to disk.

Run directly with:  python -m workflow_dna.ml.run_preprocessing
"""

import sys
from pathlib import Path

# Allow running this file directly (not just as a module) during dev.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from backend.workflow_dna.ml.preprocessing import load_and_clean
from backend.workflow_dna.ml.feature_engineering import engineer_features, CANONICAL_FEATURE_COLUMNS
from backend.workflow_dna.fitness.calculator import NormalizationStats, FitnessCalculator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DATA_PATH = PROJECT_ROOT / "dataset" / "raw" / "workflow_dna_dataset.xlsx"
PROCESSED_DIR = PROJECT_ROOT / "dataset" / "processed"
ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"


def build_training_table(raw_path: str, artifacts_dir: str) -> pd.DataFrame:
    """Runs the full Phase 2 pipeline and returns the final training table."""
    cleaned = load_and_clean(raw_path)
    features = engineer_features(cleaned, artifacts_dir)

    # Fit normalization stats on this training batch and persist them —
    # the live orchestrator's fitness scoring (added in a later phase)
    # will load this same file so training-time and inference-time
    # fitness scores stay on an identical scale.
    stats = NormalizationStats.fit(
        execution_times=features["execution_time"],
        costs=features["cost"],
    )
    stats.save(str(Path(artifacts_dir) / "fitness_normalization_stats.json"))

    calc = FitnessCalculator(stats)
    features["fitness"] = features.apply(
        lambda row: calc.compute_fitness(
            abort_count=row["_abort_count"],
            start_count=row["_start_count"],
            resume_count=row["_resume_count"],
            execution_time_seconds=row["execution_time"],
            cost=row["cost"],
        ),
        axis=1,
    )

    # Helper columns were only needed to compute fitness — drop before saving.
    features = features.drop(columns=["_abort_count", "_start_count", "_resume_count"])
    return features


def main():
    table = build_training_table(str(RAW_DATA_PATH), str(ARTIFACTS_DIR))

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PROCESSED_DIR / "workflow_dna_training_features.csv"
    table.to_csv(out_path, index=False)

    print(f"Wrote {len(table)} rows x {len(table.columns)} columns to {out_path}")
    print("\nCanonical model input features:", CANONICAL_FEATURE_COLUMNS)
    print("\nEngineered Fitness target summary:")
    print(table["fitness"].describe())
    print("\nSample rows:")
    print(table.head(5).to_string())


if __name__ == "__main__":
    main()
