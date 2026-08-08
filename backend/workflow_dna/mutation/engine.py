"""
workflow_dna/mutation/engine.py

The "lightweight Evolution Engine" the spec asks for — deliberately NOT
a genetic algorithm. No population, no crossover, no multi-generation
search. Given one WorkflowDNA, it produces exactly ONE mutated
candidate, using a fixed, explainable priority list rather than any
learned or random selection logic.

Mutation types and what they change, at this DNA-lite abstraction
level (structural counts only — Workflow DNA doesn't see the real
orchestrator graph, so "reorder agents" can't literally reorder a
step list here):

  parallelize_agents      parallel_execution: 0 -> 1
  remove_agent             agent_count -= 1, workflow_length -= 2, workflow_depth -= 1
  reorder_agents           workflow_depth -= 1 (shallower critical path proxy)
  add_agent                agent_count += 1, workflow_length += 2, workflow_depth += 1
  modify_retry_strategy    no structural DNA change — a SIGNAL mutation the
                           orchestrator itself must interpret and apply
                           (e.g. cap max retries), since retry policy is
                           an execution-engine concern outside DNA's scope
  swap_model               no structural DNA change — same reasoning;
                           which model runs isn't part of the 9 tracked
                           DNA features, so this is signal-only too

Selection priority (first eligible mutation wins — simple and
deterministic, matching the spec's "no complex evolution" instruction):

  1. parallelize_agents   — only if not already parallel
  2. remove_agent          — only if agent_count > 3 (assumes possible redundancy)
  3. reorder_agents        — only if workflow_depth > 5
  4. add_agent              — fallback, always eligible, so this engine
                             always produces a candidate

`modify_retry_strategy` and `swap_model` stay in MUTATION_TYPE_VOCABULARY
for the orchestrator to apply later (retry policy and model choice are
execution-engine concerns this lite DNA representation doesn't track
numerically), but this deterministic engine never auto-selects them:
since `add_agent` has no precondition that would ever exclude it, it
always wins before reaching them in priority order. Rather than write
unreachable branches for the sake of listing all 6, those two are
documented here as reserved for a future, richer DNA representation
that actually models retry policy or model identity as tracked fields.
"""

import copy

from backend.workflow_dna.prediction.dna import WorkflowDNA


class MutationEngine:
    def mutate(self, dna: WorkflowDNA, new_workflow_id: str) -> WorkflowDNA:
        """
        Returns exactly ONE mutated WorkflowDNA, linked to `dna` as its
        parent, with `generation` left for the caller to set (the
        history repository owns generation numbering).
        """
        mutated = copy.deepcopy(dna)
        mutated.workflow_id = new_workflow_id
        mutated.parent_workflow = dna.workflow_id

        if dna.parallel_execution == 0:
            mutated.parallel_execution = 1
            mutated.mutation_type = "parallelize_agents"
        elif dna.agent_count > 3:
            mutated.agent_count = dna.agent_count - 1
            mutated.workflow_length = max(1, dna.workflow_length - 2)
            mutated.workflow_depth = max(1, dna.workflow_depth - 1)
            mutated.mutation_type = "remove_agent"
        elif dna.workflow_depth > 5:
            mutated.workflow_depth = dna.workflow_depth - 1
            mutated.mutation_type = "reorder_agents"
        else:  # always eligible fallback — mutate() never fails to produce a candidate
            mutated.agent_count = dna.agent_count + 1
            mutated.workflow_length = dna.workflow_length + 2
            mutated.workflow_depth = dna.workflow_depth + 1
            mutated.mutation_type = "add_agent"

        return mutated
