"""
workflow_dna/fitness/calculator.py

Reusable Fitness Calculator for Workflow DNA.

Computes a single 0-100 Fitness Score for a workflow execution as a
weighted blend of four dimensions:

    40% Quality      - how cleanly the workflow completed (few aborts/errors)
    30% Speed        - how fast it ran, relative to other observed workflows
    20% Cost         - how cheap it was to run, relative to other workflows
    10% Reliability  - how few retries/interruptions it needed

This is the SAME calculator used in two different places, on purpose:

  1. Offline, during training-data preparation (this phase), to re-label
     historical executions with a fitness score that matches this
     platform's own definition, instead of trusting a foreign score.
  2. Online, inside the orchestrator (added in a later phase), every time
     a real workflow finishes executing, before that execution is written
     to Workflow DNA history.

"Speed" and "cost" are relative concepts — a 2-second run is fast for one
kind of agent and slow for another — so both are normalized against
reference statistics (NormalizationStats) fitted once on a batch of
workflows, rather than hard-coded thresholds. Those stats are saved to
disk so training-time and inference-time scoring stay on the same scale.
"""

import json
import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class NormalizationStats:
    """
    Reference bounds used to squash raw metrics into a comparable 0-1
    range before they're weighted into the fitness formula.

    Execution time is stored in log-space (log1p) because raw durations
    can span multiple orders of magnitude (the BPI2017 pretraining data
    ranges from ~10 minutes to ~135 days) — without a log transform, a
    handful of extreme-duration cases would dominate the normalization
    and flatten every "normal" case's speed score toward the same value.
    """

    log_execution_time_min: float
    log_execution_time_max: float
    cost_min: float
    cost_max: float

    def save(self, path: str) -> None:
        """Persists stats as JSON so training and inference stay in sync."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: str) -> "NormalizationStats":
        data = json.loads(Path(path).read_text())
        return cls(**data)

    @classmethod
    def fit(cls, execution_times: Iterable[float], costs: Iterable[float]) -> "NormalizationStats":
        """
        Fits min/max bounds from a batch of historical executions.
        Call this once on training data (Phase 2) and again later,
        periodically, on live orchestrator history to keep the scale
        current as real workflow behavior comes in.
        """
        times = list(execution_times)
        cost_values = list(costs)
        log_times = [math.log1p(max(t, 0)) for t in times]
        return cls(
            log_execution_time_min=min(log_times),
            log_execution_time_max=max(log_times),
            cost_min=min(cost_values),
            cost_max=max(cost_values),
        )


def _minmax(value: float, lo: float, hi: float) -> float:
    """Scales `value` into [0, 1] given a known [lo, hi] range, clipping outliers."""
    if hi <= lo:
        return 0.5  # degenerate/no-signal range — return a neutral score rather than divide by zero
    scaled = (value - lo) / (hi - lo)
    return max(0.0, min(1.0, scaled))


class FitnessCalculator:
    """
    Computes the platform's 0-100 Workflow Fitness Score from raw
    execution metrics.

    Example:
        stats = NormalizationStats.fit(execution_times, costs)
        calc = FitnessCalculator(stats)
        score = calc.compute_fitness(
            abort_count=0, start_count=5, resume_count=1,
            execution_time_seconds=42.0, cost=0.015,
        )
    """

    QUALITY_WEIGHT = 0.40
    SPEED_WEIGHT = 0.30
    COST_WEIGHT = 0.20
    RELIABILITY_WEIGHT = 0.10

    def __init__(self, stats: NormalizationStats):
        self.stats = stats

    @staticmethod
    def quality_score(abort_count: int, start_count: int) -> float:
        """
        0-1 quality: the fraction of started steps that did NOT abort.
        Zero aborts -> 1.0. A step that aborts on every attempt -> ~0.

        Static because it needs no normalization stats — it's a pure
        ratio. Exposed as a staticmethod so other modules (feature
        engineering, the prediction service) can reuse this EXACT
        formula instead of re-implementing it, keeping the platform's
        definition of "quality" in exactly one place.
        """
        attempts = max(start_count, 1)
        return max(0.0, 1.0 - (abort_count / attempts))

    def speed_score(self, execution_time_seconds: float) -> float:
        """
        0-1 speed: inverse of log-scaled execution time, normalized
        against the fitted reference range. Faster -> higher score.
        """
        log_t = math.log1p(max(execution_time_seconds, 0))
        normalized = _minmax(
            log_t, self.stats.log_execution_time_min, self.stats.log_execution_time_max
        )
        return 1.0 - normalized

    def cost_score(self, cost: float) -> float:
        """0-1 cost score: inverse of normalized cost. Cheaper -> higher score."""
        normalized = _minmax(cost, self.stats.cost_min, self.stats.cost_max)
        return 1.0 - normalized

    @staticmethod
    def reliability_score(abort_count: int, resume_count: int) -> float:
        """
        0-1 reliability: penalizes interruptions (aborts + resumes-after-
        suspend). A fully clean run with none of either scores 1.0; five
        or more combined interruptions floors the score at 0.
        """
        interruptions = abort_count + resume_count
        return max(0.0, 1.0 - (interruptions / 5.0))

    def compute_fitness(
        self,
        abort_count: int,
        start_count: int,
        resume_count: int,
        execution_time_seconds: float,
        cost: float,
    ) -> float:
        """Combines the four weighted sub-scores into the final 0-100 Fitness Score."""
        quality = self.quality_score(abort_count, start_count)
        speed = self.speed_score(execution_time_seconds)
        cost_component = self.cost_score(cost)
        reliability = self.reliability_score(abort_count, resume_count)

        weighted = (
            self.QUALITY_WEIGHT * quality
            + self.SPEED_WEIGHT * speed
            + self.COST_WEIGHT * cost_component
            + self.RELIABILITY_WEIGHT * reliability
        )
        return round(weighted * 100, 2)
