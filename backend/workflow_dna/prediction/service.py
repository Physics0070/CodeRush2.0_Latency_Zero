"""
workflow_dna/prediction/service.py

The orchestrator-facing entrypoint for "what fitness would this
workflow probably get?" — combines:

  1. HistoricalFeatureEstimator: fills in execution_time/cost/
     retry_count/success for a not-yet-executed WorkflowDNA
  2. FitnessModelService: the trained RandomForestRegressor

into a single call that returns a PredictedFitnessResult, including
WHERE the estimate came from (real history vs. cold-start prior) so
the UI can be honest about how confident the prediction is — exactly
the "Predicted Fitness: 74" style panel the spec's UI section asks for.
"""

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from backend.workflow_dna.ml.model_service import FitnessModelService
from backend.workflow_dna.ml.feature_engineering import encode_mutation_type
from backend.workflow_dna.prediction.dna import WorkflowDNA
from backend.workflow_dna.prediction.estimator import HistoricalFeatureEstimator, EstimatedOutcome


@dataclass
class PredictedFitnessResult:
    workflow_id: str
    predicted_fitness: float
    estimated_outcome: EstimatedOutcome  # includes source="history"/"cold_start" for UI transparency


class PredictionService:
    def __init__(
        self,
        model_service: Optional[FitnessModelService] = None,
        estimator: Optional[HistoricalFeatureEstimator] = None,
    ):
        self.model_service = model_service or FitnessModelService()
        self.estimator = estimator or HistoricalFeatureEstimator()

    def predict(self, dna: WorkflowDNA) -> PredictedFitnessResult:
        """Predicts fitness for a WorkflowDNA that hasn't executed yet."""
        estimated = self.estimator.estimate(dna)

        mutation_code = int(encode_mutation_type(pd.Series([dna.mutation_type])).iloc[0])

        features = {
            "workflow_length": dna.workflow_length,
            "number_of_agents": dna.agent_count,
            "parallel_enabled": dna.parallel_execution,
            "retry_count": estimated.retry_count,
            "execution_time": estimated.execution_time,
            "cost": estimated.cost,
            "success": estimated.success,
            "workflow_depth": dna.workflow_depth,
            "mutation_type_encoded": mutation_code,
        }
        predicted_fitness = self.model_service.predict_fitness(features)

        return PredictedFitnessResult(
            workflow_id=dna.workflow_id,
            predicted_fitness=predicted_fitness,
            estimated_outcome=estimated,
        )
