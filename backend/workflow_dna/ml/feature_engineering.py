"""
workflow_dna/ml/feature_engineering.py

Transforms the cleaned raw dataset into the exact 9-feature schema the
RandomForestRegressor expects. The Fitness target itself is NOT computed
here (that requires the FitnessCalculator and lives in run_preprocessing.py)
— this module's only job is structural feature engineering.

Raw column -> DNA feature mapping (decided in Phase 1 / refined in Phase 2):

    workflow_length      <- WorkflowLength           total events = total steps taken
    number_of_agents     <- UniqueResources           distinct actors = agents
    workflow_depth        <- UniqueActivities          distinct stage types = graph depth
    execution_time         <- ExecutionTimeSeconds
    cost                    <- RequestedAmount
    retry_count             <- ResumeCount + AbortCount   resume-after-suspend and
                                                          abort-and-reattempt are this
                                                          dataset's closest analogues
                                                          to an agent "retrying" a step
    success                 <- continuous quality score, see note below
    parallel_enabled         <- heuristic, see _infer_parallel_enabled()

NOTE on `success` (updated during Phase 3 model training): the first
version of this feature was a strict binary flag (AbortCount == 0).
That turned out to be a bad choice — only 7 of 1,262 rows ever have
zero aborts, so the flag was "0" almost everywhere and carried almost
no information. Since 40% of the Fitness target is itself a continuous
quality score built from the abort ratio, hiding that ratio behind an
always-0 flag meant the model couldn't see the single biggest driver of
its own target. `success` is now that same continuous 0-1 quality
score (reusing FitnessCalculator.quality_score so there's one
definition of "quality" across the whole platform, not two).
    mutation_type_encoded     <- constant "none" for every pretraining row

These three columns split what Phase 1's rougher draft mapping had
lumped together (UniqueActivities/UniqueResources both as "agent count")
so that no two canonical features measure the same thing.

LoanGoal and ApplicationType are intentionally EXCLUDED from the model's
input features. Phase 1 diagnostics showed near-zero feature importance
(<0.0003) for these columns, and they aren't part of the 9 input
features the platform spec declares. They stay in the cleaned dataset
for optional future analysis, just not passed to the regressor.
"""

from pathlib import Path
import json

import pandas as pd

from backend.workflow_dna.fitness.calculator import FitnessCalculator

# Pre-defined so the encoding is STABLE across the pretraining run and
# the live orchestrator, regardless of which mutation types have
# actually been observed yet. "none" always maps to 0. This list mirrors
# the mutation types the (later-phase) Evolution Engine is allowed to
# apply, so the encoder never needs to be refit once real mutations
# start appearing in orchestrator history.
MUTATION_TYPE_VOCABULARY = [
    "none",
    "parallelize_agents",
    "reorder_agents",
    "swap_model",
    "add_agent",
    "remove_agent",
    "modify_retry_strategy",
]

CANONICAL_FEATURE_COLUMNS = [
    "workflow_length",
    "number_of_agents",
    "parallel_enabled",
    "retry_count",
    "execution_time",
    "cost",
    "success",
    "workflow_depth",
    "mutation_type_encoded",
]


def _infer_parallel_enabled(df: pd.DataFrame) -> pd.Series:
    """
    Heuristic proxy for "was this workflow parallelized" — used ONLY for
    pretraining. The source dataset (BPI2017) is a real-world business
    process log with no literal parallel-execution flag.

    Heuristic: if a case involves MORE distinct resources than distinct
    activity types, more than one resource was likely working on parts
    of the case concurrently, rather than one resource handling every
    activity type sequentially. This is a defensible but imperfect
    proxy. Once the live orchestrator is the data source, this column
    will carry a real, ground-truth flag and this heuristic goes away.
    """
    return (df["UniqueResources"] > df["UniqueActivities"]).astype(int)


def _save_mutation_vocabulary(artifacts_dir: str) -> None:
    """Persists the fixed mutation-type vocabulary so the prediction
    service (Phase 4) encodes real mutation types the exact same way."""
    path = Path(artifacts_dir) / "mutation_type_vocabulary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(MUTATION_TYPE_VOCABULARY, indent=2))


def encode_mutation_type(values: pd.Series) -> pd.Series:
    """Maps mutation-type strings to their fixed integer code."""
    mapping = {name: idx for idx, name in enumerate(MUTATION_TYPE_VOCABULARY)}
    unknown = set(values.unique()) - set(mapping)
    if unknown:
        raise ValueError(f"Unrecognized mutation type(s): {unknown}. Update MUTATION_TYPE_VOCABULARY.")
    return values.map(mapping)


def engineer_features(df: pd.DataFrame, artifacts_dir: str) -> pd.DataFrame:
    """
    Produces the training table: workflow_id + the 9 canonical features,
    plus three underscore-prefixed helper columns (_abort_count,
    _start_count, _resume_count) that run_preprocessing.py needs to
    compute the Fitness target. Those helper columns are dropped before
    the table is saved — they are not model inputs.
    """
    out = pd.DataFrame()
    out["workflow_id"] = df["WorkflowID"]

    out["workflow_length"] = df["WorkflowLength"]
    out["number_of_agents"] = df["UniqueResources"]
    out["workflow_depth"] = df["UniqueActivities"]
    out["execution_time"] = df["ExecutionTimeSeconds"]
    out["cost"] = df["RequestedAmount"]
    out["retry_count"] = df["ResumeCount"] + df["AbortCount"]
    out["success"] = df.apply(
        lambda row: FitnessCalculator.quality_score(row["AbortCount"], row["StartCount"]),
        axis=1,
    )
    out["parallel_enabled"] = _infer_parallel_enabled(df)

    mutation_series = pd.Series(["none"] * len(df), index=df.index)
    _save_mutation_vocabulary(artifacts_dir)
    out["mutation_type_encoded"] = encode_mutation_type(mutation_series)

    # Carried through only so the Fitness target can be computed downstream.
    out["_abort_count"] = df["AbortCount"]
    out["_start_count"] = df["StartCount"]
    out["_resume_count"] = df["ResumeCount"]

    return out
