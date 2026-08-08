"""
workflow_dna/ml/preprocessing.py

Loads the raw Workflow DNA training dataset and cleans it according to
the data-quality findings from Phase 1:

  - drops columns with zero information content (fully-null or constant)
  - drops an exact-duplicate column
  - validates dtypes and value ranges

This module only touches the standalone training dataset used to
pretrain the fitness predictor. It knows nothing about the orchestrator
or about live workflow executions.
"""

from pathlib import Path

import pandas as pd

# Phase 1 finding: these 4 columns are 100% null across all 1,262 rows
# (offer-stage fields that only populate for accepted loans — every row
# in this dataset happens to be a non-accepted case), and "Accepted" is
# constant (always 0) for the same reason. All five carry zero
# information for modeling and must be dropped.
DEAD_COLUMNS = ["NumberOfTerms", "MonthlyCost", "CreditScore", "OfferedAmount", "Accepted"]

# Phase 1 finding: SuspendCount and ReleasedCount are byte-for-byte
# identical in every row (max absolute difference == 0 across all 1,262
# rows). Keeping both would double-count the same signal.
DUPLICATE_COLUMNS = ["SuspendCount"]

REQUIRED_COLUMNS = [
    "WorkflowID", "WorkflowLength", "UniqueActivities", "UniqueResources",
    "ExecutionTimeSeconds", "CreatedCount", "DeletedCount", "StateChangeCount",
    "ObtainedCount", "ReleasedCount", "CompleteCount", "StartCount",
    "SuspendCount", "ResumeCount", "AbortCount", "RequestedAmount",
    "LoanGoal", "ApplicationType",
]


def load_raw_dataset(path: str) -> pd.DataFrame:
    """Loads the raw .xlsx training dataset and validates required columns exist."""
    if not Path(path).exists():
        raise FileNotFoundError(f"Training dataset not found at {path}")
    df = pd.read_excel(path)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Dataset is missing expected columns: {missing}")
    return df


def clean_dataset(df: pd.DataFrame) -> pd.DataFrame:
    """
    Applies the Phase 1 data-quality findings:
      1. Drop dead columns (fully-null / constant).
      2. Drop the exact-duplicate column.
      3. Sanity-check remaining numeric columns for negative values that
         shouldn't exist (counts, durations, amounts must all be >= 0) —
         a defensive check in case future dataset refreshes are dirtier
         than this one.
    """
    df = df.copy()

    to_drop = [c for c in DEAD_COLUMNS + DUPLICATE_COLUMNS if c in df.columns]
    df = df.drop(columns=to_drop)

    numeric_cols = [c for c in df.select_dtypes(include="number").columns]
    for col in numeric_cols:
        negative_count = (df[col] < 0).sum()
        if negative_count > 0:
            raise ValueError(
                f"Column '{col}' has {negative_count} negative values — data integrity issue."
            )

    return df


def load_and_clean(path: str) -> pd.DataFrame:
    """Convenience wrapper: load + clean in one call."""
    return clean_dataset(load_raw_dataset(path))
