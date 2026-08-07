export type EventType =
  | 'RUN_START' | 'CLARIFY_ASKED' | 'CLARIFY_ANSWERED' | 'GRAPH_PROPOSED'
  | 'GRAPH_EDITED' | 'GRAPH_LOCKED' | 'GRAPH_APPROVED' | 'NODE_START' | 'NODE_END'
  | 'HANDOFF_EMITTED' | 'HANDOFF_VALIDATED' | 'HANDOFF_REJECTED' | 'REPAIR_ATTEMPT'
  | 'TOOL_CALL' | 'TOOL_RESULT' | 'TOOL_BLOCKED' | 'INJECTION_BLOCKED'
  | 'BUDGET_EXCEEDED' | 'RETRY' | 'COMPENSATE' | 'COUNCIL_PROPOSAL'
  | 'COUNCIL_RANKING' | 'COUNCIL_VERDICT' | 'APPROVAL_REQUESTED' | 'APPROVAL_GRANTED'
  | 'PAUSE' | 'RESUME' | 'CANCEL' | 'BRANCH_FAILED' | 'RUN_END'

export interface LogEvent {
  run_id: string
  seq: number
  event_type: EventType
  node_id: string | null
  agent_id: string | null
  payload: Record<string, unknown> | null
  tokens_in: number
  tokens_out: number
  cost_usd: number
  latency_ms: number | null
  ts: string | null
}

export interface AgentSpec {
  id: string; role: string; system_contract: string
  tools: string[]; model: string; fallback_model: string | null
  budget_tokens: number; timeout_s: number; allowed_side_effects: string[]
}

export interface GraphNode {
  id: string
  type: 'clarify' | 'agent' | 'fanout' | 'join' | 'conditional' | 'verify'
      | 'council' | 'approval' | 'compensate' | 'subgraph'
  agent: AgentSpec | null
  compensates?: string | null
  max_retries: number
}

export interface GraphEdge { from_node: string; to_node: string; handoff_schema: Record<string, unknown> }
export interface GraphSpec {
  version: number; config_hash: string
  nodes: GraphNode[]; edges: GraphEdge[]; locked: boolean
}

export interface AgentMetric {
  agent_id: string; node_id: string
  marginal_information_gain: number
  tokens: number; cost_usd: number; findings: number
}

export interface RunMetrics {
  run_id: string
  agents: AgentMetric[]
  redundancy_index: number
  duplicate_pairs: { a: string; b: string; cosine: number }[]
  duplicate_work: number
  parallel_efficiency: number
  recovery_rate: number | null
  handoff_validity: number | null
  branch_utilization: number
  approval_frequency: number
  total_cost_usd: number
  total_tokens: number
  unique_findings: number
  cost_per_unique_finding: number | null
  tokens_per_unique_finding: number | null
  least_valuable_agent: string | null
  verdict: string
}

export interface ReplayDiff {
  identical: boolean
  output_diffs: { node: string; original: unknown; replay: unknown }[]
  original_cost_usd: number; replay_cost_usd: number
  original_wall_ms: number; replay_wall_ms: number
  original_tokens: number; replay_tokens: number
  nodes_compared: number
}

export interface DepthPoint {
  depth: number; agents: string[]; unique_findings: number
  total_cost_usd: number; total_tokens: number; wall_clock_ms: number
  redundancy_index: number; duplicate_work: number; parallel_efficiency: number
  lowest_mig: number; lowest_mig_agent: string
  quality_per_1k_tokens: number; quality_per_rupee: number | null
}

export interface MarginalValueReport {
  goal: string; points: DepthPoint[]; best_depth: number
  recommendation: string; cost_basis: string
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!r.ok) {
    const body = await r.text()
    throw new Error(`${r.status}: ${body.slice(0, 300)}`)
  }
  return r.json() as Promise<T>
}

export const api = {
  models: () => req<{ models: string[]; providers: string[] }>('/api/models'),

  clarify: (goal: string) =>
    req<{ questions: { id: string; text: string }[]; permission: string }>(
      '/api/clarify', { method: 'POST', body: JSON.stringify({ goal }) }),

  compile: (goal: string, answers: Record<string, string>, models: string[]) =>
    req<{ run_id: string; graph: GraphSpec; council: CouncilSummary | null }>(
      '/api/compile', { method: 'POST', body: JSON.stringify({ goal, answers, models }) }),

  start: (goal: string, graph: GraphSpec, approvals: string[]) =>
    req<{ run_id: string; config_hash: string }>(
      '/api/runs', { method: 'POST', body: JSON.stringify({ goal, graph, approvals }) }),

  events: (runId: string) => req<{ events: LogEvent[] }>(`/api/runs/${runId}/events`),
  metrics: (runId: string) => req<RunMetrics>(`/api/runs/${runId}/metrics`),
  replay: (runId: string) =>
    req<{ replay_run_id: string; diff: ReplayDiff; cost_saved_usd: number }>(
      `/api/runs/${runId}/replay`, { method: 'POST' }),
  cancel: (runId: string) => req(`/api/runs/${runId}/cancel`, { method: 'POST' }),
  pause: (runId: string) => req(`/api/runs/${runId}/pause`, { method: 'POST' }),
  resume: (runId: string) => req(`/api/runs/${runId}/resume`, { method: 'POST' }),
  redAgent: () =>
    req<{ run_id: string; tool_blocked: boolean; injection_blocked: boolean; events: LogEvent[] }>(
      '/api/demo/red-agent', { method: 'POST' }),
  marginalValue: (goal: string, models: string[]) =>
    req<MarginalValueReport>('/api/marginal-value',
      { method: 'POST', body: JSON.stringify({ goal, models, depths: [1, 2, 3, 4] }) }),
}

export interface CouncilSummary {
  chairman: string
  borda: Record<string, number>
  winner_label: string
  winner_author: string
  disagreement: number
  escalated: boolean
  proposals: { label: string; author_model: string; tokens: number }[]
  rankings: { ranker: string; order: string[] }[]
}

/** Live tail of the event log. One connection, server-side polling. */
export function streamEvents(
  runId: string,
  onEvent: (e: LogEvent) => void,
  onDone: () => void,
): () => void {
  const es = new EventSource(`/api/runs/${runId}/stream`)
  es.addEventListener('log', (m) => onEvent(JSON.parse((m as MessageEvent).data)))
  es.addEventListener('done', () => { es.close(); onDone() })
  es.onerror = () => { es.close(); onDone() }
  return () => es.close()
}
