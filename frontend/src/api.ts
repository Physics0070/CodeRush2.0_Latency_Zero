export type EventType =
  | 'RUN_START' | 'CLARIFY_ASKED' | 'CLARIFY_ANSWERED' | 'GRAPH_PROPOSED'
  | 'GRAPH_EDITED' | 'GRAPH_LOCKED' | 'GRAPH_APPROVED' | 'NODE_START' | 'NODE_END'
  | 'HANDOFF_EMITTED' | 'HANDOFF_VALIDATED' | 'HANDOFF_REJECTED' | 'REPAIR_ATTEMPT'
  | 'TOOL_CALL' | 'TOOL_RESULT' | 'TOOL_BLOCKED' | 'INJECTION_BLOCKED'
  | 'BUDGET_EXCEEDED' | 'RETRY' | 'COMPENSATE' | 'COUNCIL_PROPOSAL'
  | 'COUNCIL_RANKING' | 'COUNCIL_VERDICT' | 'APPROVAL_REQUESTED' | 'APPROVAL_GRANTED'
  | 'PAUSE' | 'RESUME' | 'CANCEL' | 'BRANCH_FAILED' | 'RUN_END'
  | 'MEMORY_WRITE' | 'MEMORY_READ' | 'MEMORY_EVICTED'

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
  memory_ttl_s?: number | null
}

export interface GraphNode {
  id: string
  type: 'clarify' | 'agent' | 'fanout' | 'join' | 'conditional' | 'verify'
      | 'council' | 'approval' | 'compensate' | 'subgraph'
  agent: AgentSpec | null
  compensates?: string | null
  max_retries: number
  subgraph?: GraphSpec | null
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

/**
 * Empty by default: FastAPI serves this bundle from the same origin, so relative
 * paths just work. Set VITE_API_BASE when the frontend is hosted separately
 * (Vercel/Netlify static + the backend on a container host) — the backend cannot
 * run on a serverless platform, since SSE and multi-minute runs outlive a
 * function invocation.
 */
export const API_BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

const url = (path: string) => `${API_BASE}${path}`

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url(path), {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!r.ok) {
    const body = await r.text()
    let detail = body.slice(0, 300)
    try {
      const parsed = JSON.parse(body)
      detail = parsed.message ?? parsed.detail ?? detail
      if (typeof detail === 'object') detail = JSON.stringify(detail)
    } catch {
      /* body was not JSON; the raw slice is the best we have */
    }
    throw new Error(`${r.status} · ${detail}`)
  }
  return r.json() as Promise<T>
}

export const api = {
  models: () => req<{ models: string[]; providers: string[] }>('/api/models'),

  clarify: (goal: string) =>
    req<{
      questions: { id: string; text: string }[]
      permission: string
      plan: {
        intent: string; complexity: string; rationale: string; source: string
        specialists: { id: string; role: string }[]
      }
    }>('/api/clarify', { method: 'POST', body: JSON.stringify({ goal }) }),

  compile: (goal: string, answers: Record<string, string>, models: string[], intent?: string) =>
    req<{ run_id: string; graph: GraphSpec; council: CouncilSummary | null }>(
      '/api/compile',
      { method: 'POST', body: JSON.stringify({ goal, answers, models, intent }) }),

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

// ------------------------------------------------------------------ chat

export interface ChatMessage { role: 'user' | 'assistant'; content: string }

export interface PlanInfo {
  intent: string
  complexity: 'simple' | 'moderate' | 'deep'
  rationale: string
  source: string
  route: 'fast' | 'council'
  clarifying_questions: string[]
  specialists: { id: string; role: string }[]
  plan_ms: number
  model_choices: { id: string; role: string; model: string; reason: string }[]
  answer_model: string
  answer_model_reason: string
  // Nodes/edges only for a chat turn's graph - no config_hash/locked meaning
  // applies (never approved, never re-run in place, unlike an Orchestrator run).
  graph: { nodes: GraphNode[]; edges: GraphEdge[] }
}

export interface SpecialistInfo {
  id: string; points: number; model: string | null; reason: string | null
  latency_ms: number | null; tokens: number | null; error: string | null
}

export interface Benchmarks {
  run_id: string
  route: 'fast' | 'council'
  intent: string
  complexity: string
  planner_source: string
  models_used: string[]
  answer_model: string
  answer_model_reason: string
  timing: {
    plan_ms: number; fanout_ms: number
    first_token_ms: number | null; total_ms: number
  }
  specialists: {
    id: string; points: number; model: string | null; reason: string | null
    latency_ms: number | null; tokens: number | null; failed: boolean
  }[]
  parallel_efficiency: number | null
  tokens: number
  cost_usd: number
  answer_chars: number
  marginal_information_gain: {
    available: boolean
    per_agent: Record<string, number>
    overlapping_pairs: { a: string; b: string; similarity: number }[]
  }
}

export interface ChatHandlers {
  onPlan?: (p: PlanInfo) => void
  onStatus?: (stage: string) => void
  onSpecialist?: (s: SpecialistInfo) => void
  onToken?: (t: string) => void
  onBenchmarks?: (b: Benchmarks) => void
  onError?: (detail: string) => void
  /** Fires once when the stream closes, however it closed. */
  onDone?: () => void
}

/**
 * Streams a chat turn.
 *
 * `EventSource` is GET-only and the turn carries history in the body, so the
 * SSE frames are parsed off a fetch stream here instead. Returns an abort
 * function so a user can stop a long answer.
 */
export function streamChat(
  body: { message: string; history: ChatMessage[]; models?: string[] },
  h: ChatHandlers,
): () => void {
  const ctrl = new AbortController()

  ;(async () => {
    try {
      const r = await fetch(url('/api/chat'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      })
      if (!r.ok || !r.body) {
        throw new Error(`${r.status} · ${(await r.text()).slice(0, 200)}`)
      }

      const reader = r.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''

      const dispatch = (frame: string) => {
        let name = 'message'
        const dataLines: string[] = []
        for (const line of frame.split('\n')) {
          if (line.startsWith('event:')) name = line.slice(6).trim()
          else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
        }
        if (!dataLines.length) return

        let data: unknown
        try { data = JSON.parse(dataLines.join('\n')) } catch { return }

        switch (name) {
          case 'plan': h.onPlan?.(data as PlanInfo); break
          case 'status': h.onStatus?.((data as { stage: string }).stage); break
          case 'specialist': h.onSpecialist?.(data as SpecialistInfo); break
          case 'token': h.onToken?.(data as string); break
          case 'benchmarks': h.onBenchmarks?.(data as Benchmarks); break
          case 'error': h.onError?.((data as { detail: string }).detail); break
        }
      }

      for (;;) {
        const { done, value } = await reader.read()
        if (done) break

        // The server terminates SSE lines with CRLF, so splitting raw on '\n\n'
        // never matches - the \r sits between the two newlines and every frame
        // stays stuck in the buffer. Normalise first, then split. A chunk can
        // end mid-CRLF; the lone trailing \r is simply carried to the next pass.
        buf = (buf + decoder.decode(value, { stream: true })).replace(/\r\n/g, '\n')

        // SSE frames are separated by a blank line. Keep the trailing partial
        // frame in the buffer until its terminator arrives.
        const frames = buf.split('\n\n')
        buf = frames.pop() ?? ''

        for (const frame of frames) dispatch(frame)
      }

      // A final frame may arrive without its blank-line terminator.
      buf = (buf + decoder.decode()).replace(/\r\n/g, '\n')
      if (buf.trim()) dispatch(buf)
      h.onDone?.()
    } catch (e) {
      if ((e as Error).name !== 'AbortError') h.onError?.((e as Error).message)
      h.onDone?.()
    }
  })()

  return () => ctrl.abort()
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
  const es = new EventSource(url(`/api/runs/${runId}/stream`))
  es.addEventListener('log', (m) => onEvent(JSON.parse((m as MessageEvent).data)))
  es.addEventListener('done', () => { es.close(); onDone() })
  es.onerror = () => { es.close(); onDone() }
  return () => es.close()
}
