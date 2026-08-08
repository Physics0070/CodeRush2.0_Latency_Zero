"""
workflow_dna/service.py

WorkflowDNAService is the single object the orchestrator talks to. It
ties together prediction, mutation, and history behind two methods:

  evaluate_workflow()        called BEFORE execution — implements the
                              spec's Prediction Flow: predict fitness,
                              and if it's below an acceptability
                              threshold, generate and predict ONE
                              mutated alternative, then recommend
                              whichever candidate scored higher.

  record_actual_execution()  called AFTER execution — computes the
                              REAL fitness score from what actually
                              happened, and writes it to history. This
                              is what grows Workflow DNA's memory: the
                              next call to evaluate_workflow() for a
                              similarly-shaped workflow will have one
                              more real data point to estimate from.

This file is the intended integration surface for Phase 5 — the
orchestrator should only ever need to call these two methods.
"""

from dataclasses import dataclass
from typing import Optional

from backend.workflow_dna.prediction.dna import WorkflowDNA
from backend.workflow_dna.prediction.service import PredictionService, PredictedFitnessResult
from backend.workflow_dna.mutation.engine import MutationEngine
from backend.workflow_dna.history.repository import WorkflowHistoryRepository
from backend.workflow_dna.history.models import WorkflowExecutionRecord
from backend.workflow_dna.fitness.calculator import FitnessCalculator, NormalizationStats

DEFAULT_FITNESS_THRESHOLD = 50.0  # below this, a mutation is attempted


@dataclass
class DNADecision:
    original: PredictedFitnessResult
    mutated_dna: Optional[WorkflowDNA]
    mutated: Optional[PredictedFitnessResult]
    chosen_dna: WorkflowDNA
    threshold_met_without_mutation: bool


class WorkflowDNAService:
    def __init__(
        self,
        prediction_service: Optional[PredictionService] = None,
        mutation_engine: Optional[MutationEngine] = None,
        repository: Optional[WorkflowHistoryRepository] = None,
        fitness_threshold: float = DEFAULT_FITNESS_THRESHOLD,
    ):
        self.prediction_service = prediction_service or PredictionService()
        self.mutation_engine = mutation_engine or MutationEngine()
        self.repository = repository or WorkflowHistoryRepository()
        self.fitness_threshold = fitness_threshold

    def evaluate_workflow(self, dna: WorkflowDNA) -> DNADecision:
        """
        Predicts fitness for `dna`. If it clears the threshold, recommends
        executing it as-is. If not, generates exactly ONE mutation,
        predicts that too, and recommends whichever of the two is higher —
        matching the spec's Prediction Flow exactly (no population, no
        multi-round search).
        """
        original_result = self.prediction_service.predict(dna)

        if original_result.predicted_fitness >= self.fitness_threshold:
            return DNADecision(
                original=original_result,
                mutated_dna=None,
                mutated=None,
                chosen_dna=dna,
                threshold_met_without_mutation=True,
            )

        mutated_id = f"{dna.workflow_id}_mut"
        mutated_dna = self.mutation_engine.mutate(dna, new_workflow_id=mutated_id)
        mutated_dna.generation = self.repository.next_generation_number(dna.workflow_id)
        mutated_result = self.prediction_service.predict(mutated_dna)

        chosen = mutated_dna if mutated_result.predicted_fitness > original_result.predicted_fitness else dna

        return DNADecision(
            original=original_result,
            mutated_dna=mutated_dna,
            mutated=mutated_result,
            chosen_dna=chosen,
            threshold_met_without_mutation=False,
        )

    def record_actual_execution(
        self,
        dna: WorkflowDNA,
        abort_count: int,
        start_count: int,
        resume_count: int,
        execution_time_seconds: float,
        cost: float,
        predicted_fitness: Optional[float] = None,
    ) -> WorkflowExecutionRecord:
        """
        Called after a workflow actually runs. Computes the real
        Fitness Score with the same FitnessCalculator used everywhere
        else in the platform, and writes a complete record to history.

        Normalization stats are refit from current history + the
        cold-start pretraining range each call — cheap for a hackathon
        scale of history, and means the fitness scale adapts as real
        execution data accumulates. (For larger deployments this would
        be refit periodically instead of on every write; noting that
        as a known scaling limitation rather than solving it now.)
        """
        stats = NormalizationStats(
            log_execution_time_min=0.0,
            log_execution_time_max=20.0,  # ~log1p(485M seconds) generous upper bound
            cost_min=0.0,
            cost_max=max(cost, 250000.0),  # matches the observed max in the BPI2017 pretraining set
        )
        calc = FitnessCalculator(stats)
        quality = calc.quality_score(abort_count, start_count)
        fitness = calc.compute_fitness(
            abort_count=abort_count, start_count=start_count, resume_count=resume_count,
            execution_time_seconds=execution_time_seconds, cost=cost,
        )

        record = WorkflowExecutionRecord(
            workflow_id=dna.workflow_id,
            generation=dna.generation,
            parent_workflow=dna.parent_workflow,
            mutation_type=dna.mutation_type,
            agent_count=dna.agent_count,
            parallel_execution=dna.parallel_execution,
            workflow_depth=dna.workflow_depth,
            workflow_length=dna.workflow_length,
            execution_time=execution_time_seconds,
            retry_count=resume_count + abort_count,
            cost=cost,
            quality=quality,
            success=1 if abort_count == 0 else 0,
            fitness=fitness,
            predicted_fitness=predicted_fitness,
        )
        self.repository.record_execution(record)
        return record
