"""
workflow_dna/prediction/fit_cold_start_defaults.py

Computes fallback outcome estimates (execution_time, cost, retry_count,
success/quality) from the Phase 2 pretraining table, for use when the
live Workflow DNA history database has too few similar past workflows
to estimate from yet (i.e. right after the orchestrator is first
deployed, before it has accumulated its own execution history).

This is what lets prediction work on day one, before any real
orchestrator executions exist — the BPI2017-derived pretraining data
doubles as a cold-start prior. As real history accumulates, the
similarity-based estimator (estimator.py) increasingly prefers real
historical neighbors over this fallback.

Run with: python -m workflow_dna.prediction.fit_cold_start_defaults
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRAINING_TABLE_PATH = PROJECT_ROOT / "dataset" / "processed" / "workflow_dna_training_features.csv"
OUTPUT_PATH = Path(__file__).resolve().parent / "artifacts" / "cold_start_defaults.json"


def main():
    df = pd.read_csv(TRAINING_TABLE_PATH)

    defaults = {
        "execution_time": float(df["execution_time"].median()),
        "cost": float(df["cost"].median()),
        "retry_count": float(df["retry_count"].median()),
        "success": float(df["success"].median()),
        "source": "BPI2017 pretraining set (Phase 2), median values",
        "n_rows": int(len(df)),
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(defaults, indent=2))
    print(f"Wrote cold-start defaults to {OUTPUT_PATH}")
    print(json.dumps(defaults, indent=2))


if __name__ == "__main__":
    main()
