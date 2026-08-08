"""
workflow_dna
============

An intelligence layer that sits ON TOP of an existing agentic-orchestrator
platform. It never touches orchestration logic — it only reads execution
data the orchestrator already produces, and writes back a recommendation
(a predicted fitness score, and optionally one mutated alternative
workflow) that the orchestrator's caller can choose to act on or ignore.

Design contract: this package must be safely deletable. If you remove
the `workflow_dna/` folder entirely, the orchestrator keeps working
exactly as it did before Workflow DNA existed — nothing outside this
folder should import anything Workflow-DNA-specific in a way that
breaks without it.

Sub-packages (built up phase by phase — see project plan):

    fitness/     Reusable 0-100 Fitness Score calculator (Phase 2)
    ml/          Preprocessing, feature engineering, training, and the
                 fitness-prediction model service (Phases 2-3)
    prediction/  Live prediction service used by the orchestrator at
                 workflow-generation time (Phase 4)
    mutation/    Lightweight single-mutation Evolution Engine (Phase 4)
    history/     Workflow DNA database/model — generations, lineage,
                 execution history (Phase 4)
"""

__version__ = "0.1.0"
