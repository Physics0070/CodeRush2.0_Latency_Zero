import { useCallback, useEffect, useRef, useState } from 'react'
import {
  api, streamEvents,
  type CouncilSummary, type GraphSpec, type LogEvent,
  type MarginalValueReport, type ReplayDiff, type RunMetrics,
} from './api'
import { ChatPanel } from './ChatPanel'
import { ClarifyPanel, type Clarification } from './ClarifyPanel'
import { GraphCanvas, StatusLegend, type NodeStatus } from './GraphCanvas'
import { MetricsPanel } from './MetricsPanel'
import { TraceViewer } from './TraceViewer'
import { WorkflowDNAPanel } from './WorkflowDNAPanel'
import { Badge, Button, Empty, Panel, SkeletonRows, StepRail } from './ui'

const DEMO_GOAL = 'Audit this repository and produce a prioritized remediation report.'

/** Node status is derived from the event log, never tracked separately. */
function deriveStatuses(events: LogEvent[]): Record<string, NodeStatus> {
  const s: Record<string, NodeStatus> = {}
  for (const e of events) {
    const n = e.node_id
    if (!n) continue
    switch (e.event_type) {
      case 'NODE_START': s[n] = 'running'; break
      case 'NODE_END': if (s[n] !== 'failed' && s[n] !== 'blocked') s[n] = 'done'; break
      case 'BRANCH_FAILED': case 'BUDGET_EXCEEDED': s[n] = 'failed'; break
      case 'TOOL_BLOCKED': case 'INJECTION_BLOCKED': s[n] = 'blocked'; break
      case 'APPROVAL_REQUESTED': if (!s[n]) s[n] = 'running'; break
      case 'APPROVAL_GRANTED': s[n] = 'done'; break
      case 'COMPENSATE': s[n] = 'done'; break
    }
  }
  return s
}

export default function App() {
  const [goal, setGoal] = useState(DEMO_GOAL)
  const [models, setModels] = useState<string[]>([])
  const [clarification, setClarification] = useState<Clarification | null>(null)
  const [graph, setGraph] = useState<GraphSpec | null>(null)
  const [council, setCouncil] = useState<CouncilSummary | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [events, setEvents] = useState<LogEvent[]>([])
  const [live, setLive] = useState(false)
  const [metrics, setMetrics] = useState<RunMetrics | null>(null)
  const [report, setReport] = useState<MarginalValueReport | null>(null)
  const [replay, setReplay] = useState<{ diff: ReplayDiff; id: string } | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<'trace' | 'metrics' | 'dna'>('trace')
  // Chat is the default surface: the product answers questions, and the
  // orchestrator view explains how. The old flow is unchanged behind the tab.
  const [mode, setMode] = useState<'chat' | 'orchestrator'>('chat')
  const stopRef = useRef<(() => void) | null>(null)

  useEffect(() => {
    api.models().then((m) => setModels(m.models)).catch(() => {})
    return () => stopRef.current?.()
  }, [])

  const guard = useCallback(async (label: string, fn: () => Promise<void>) => {
    setBusy(label); setError(null)
    try { await fn() } catch (e) { setError((e as Error).message) } finally { setBusy(null) }
  }, [])

  const statuses = deriveStatuses(events)
  const locked = graph?.locked ?? false

  const step = metrics || report ? 4 : runId ? 3 : graph ? 2 : clarification ? 1 : 0

  const askClarify = () => guard('clarify', async () => {
    setGraph(null); setRunId(null); setEvents([]); setMetrics(null); setReplay(null)
    setReport(null); setCouncil(null); setSelected(null)
    setClarification(await api.clarify(goal))
  })

  const compile = (answers: Record<string, string>) => guard('compile', async () => {
    const r = await api.compile(goal, answers, models)
    setGraph(r.graph); setCouncil(r.council); setClarification(null)
  })

  const lock = () => graph && setGraph({ ...graph, locked: !graph.locked })

  const run = () => guard('run', async () => {
    if (!graph) return
    setEvents([]); setMetrics(null); setReplay(null); setTab('trace')
    const approvals = graph.nodes.filter((n) => n.type === 'approval').map((n) => n.id)
    const { run_id } = await api.start(goal, graph, approvals)
    setRunId(run_id); setLive(true)
    stopRef.current?.()
    stopRef.current = streamEvents(
      run_id,
      (e) => setEvents((prev) => (prev.some((x) => x.seq === e.seq) ? prev : [...prev, e])),
      () => {
        setLive(false)
        api.metrics(run_id).then(setMetrics).catch(() => {})
      },
    )
  })

  const redAgent = () => guard('red', async () => {
    const r = await api.redAgent()
    setRunId(r.run_id); setGraph(null); setMetrics(null); setClarification(null); setTab('trace')
    setEvents((await api.events(r.run_id)).events)
  })

  const doReplay = () => runId && guard('replay', async () => {
    setReplay({ diff: (await api.replay(runId)).diff, id: runId })
  })

  const doMarginal = () => guard('marginal', async () => {
    setTab('metrics')
    setReport(await api.marginalValue(goal, models))
  })

  const alerts = events.filter(
    (e) => e.event_type === 'TOOL_BLOCKED' || e.event_type === 'INJECTION_BLOCKED',
  ).length

  return (
    <div className="h-screen flex flex-col">
      {/* ---------------------------------------------------------- header */}
      <header className="shrink-0 border-b border-[var(--color-line)] bg-[var(--color-surface-1)]/70 backdrop-blur">
        <div className="flex items-center gap-3 px-4 py-2.5 flex-wrap">
          <div className="flex items-center gap-2.5 min-w-0">
            <span
              aria-hidden
              className="grid place-items-center h-7 w-7 rounded-lg bg-[var(--color-accent)] text-[#032420] text-[13px] font-bold"
            >
              ⌘
            </span>
            <div className="min-w-0">
              <h1 className="text-[14px] font-semibold leading-tight">
                Agent Council Orchestrator
              </h1>
              <p className="text-[10px] text-[var(--color-ink-faint)] font-mono leading-tight">
                AE-03 · Latency Zero
              </p>
            </div>
          </div>

          <div className="ml-4 flex items-center gap-1 p-0.5 rounded-[9px] bg-[var(--color-surface-2)] border border-[var(--color-line)]">
            {(['chat', 'orchestrator'] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`px-3 h-7 rounded-[7px] text-[12px] capitalize transition-colors ${
                  mode === m
                    ? 'bg-[var(--color-surface-4)] text-[var(--color-ink)]'
                    : 'text-[var(--color-ink-faint)] hover:text-[var(--color-ink-muted)]'
                }`}
              >
                {m}
              </button>
            ))}
          </div>

          {mode === 'orchestrator' && (
            <div className="hidden lg:block ml-2"><StepRail current={step} /></div>
          )}

          <div className="ml-auto flex items-center gap-2 flex-wrap">
            {mode === 'orchestrator' && graph && (
              <Badge tone="accent">{graph.config_hash.slice(0, 10)}</Badge>
            )}
            {mode === 'orchestrator' && live && (
              <span className="inline-flex items-center gap-1.5 text-[11px] text-[var(--color-running)]">
                <span className="h-1.5 w-1.5 rounded-full bg-current animate-pulse" />
                streaming
              </span>
            )}
            {mode === 'orchestrator' && alerts > 0 && (
              <Badge tone="bad">{alerts} blocked</Badge>
            )}
            {mode === 'orchestrator' && (
              <Button
                onClick={redAgent} variant="danger" loading={busy === 'red'} disabled={!!busy}
                title="Undeclared tool call and a smuggled instruction, both blocked"
              >
                Red Agent
              </Button>
            )}
          </div>
        </div>

        {/* ------------------------------------------------------ goal bar */}
        {mode === 'orchestrator' && <form
          className="flex items-center gap-2 px-4 pb-3 flex-wrap"
          onSubmit={(e) => { e.preventDefault(); askClarify() }}
        >
          <div className="relative flex-1 min-w-[260px]">
            <input
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              aria-label="Goal"
              placeholder="Describe your goal in plain English"
              className="w-full min-h-11 pl-3 pr-16 rounded-[10px] bg-[var(--color-surface-2)] border border-[var(--color-line)] text-[13px] placeholder:text-[var(--color-ink-faint)] focus:border-[var(--color-accent)] outline-none transition-colors"
            />
            <span className="absolute right-3 top-1/2 -translate-y-1/2 text-[10px] text-[var(--color-ink-faint)] tabular-nums pointer-events-none">
              {goal.trim().length}
            </span>
          </div>

          <Button
            type="submit" variant="primary" loading={busy === 'clarify'}
            disabled={!!busy || goal.trim().length < 10}
          >
            Start
          </Button>

          {graph && (
            <>
              <Button onClick={lock} title={locked ? 'Unlock the graph' : 'Freeze this graph'}>
                {locked ? '🔒 Locked' : 'Lock'}
              </Button>
              <Button onClick={run} variant="primary" loading={busy === 'run'} disabled={!!busy || live}>
                Approve &amp; run
              </Button>
            </>
          )}
          {runId && !live && (
            <>
              <Button onClick={doReplay} loading={busy === 'replay'} disabled={!!busy}>
                Replay
              </Button>
              <Button onClick={doMarginal} loading={busy === 'marginal'} disabled={!!busy}>
                {busy === 'marginal' ? 'depths 1-4…' : 'Marginal value'}
              </Button>
            </>
          )}
          {live && runId && (
            <Button onClick={() => api.cancel(runId)} variant="danger">Cancel</Button>
          )}
        </form>}
      </header>

      {mode === 'chat' && <ChatPanel models={models} />}

      {/* ----------------------------------------------------------- alerts */}
      {mode === 'orchestrator' && error && (
        <div
          role="alert"
          className="shrink-0 flex items-start gap-2 px-4 py-2 bg-[#f8717114] border-b border-[#f8717144] text-[12px] text-[var(--color-failed)]"
        >
          <span aria-hidden>⚠</span>
          <span className="flex-1 break-words">{error}</span>
          <button
            onClick={() => setError(null)}
            aria-label="Dismiss error"
            className="shrink-0 opacity-60 hover:opacity-100 px-1"
          >
            ✕
          </button>
        </div>
      )}

      {mode === 'orchestrator' && replay && (
        <div className="shrink-0 px-4 py-2 border-b border-[var(--color-line)] bg-[var(--color-surface-2)] flex items-center gap-3 flex-wrap text-[12px]">
          <Badge tone={replay.diff.identical ? 'good' : 'bad'}>
            {replay.diff.identical ? '✓ DIFF IS ZERO' : `${replay.diff.output_diffs.length} DIFFS`}
          </Badge>
          <span className="text-[var(--color-ink-muted)]">
            {replay.diff.nodes_compared} nodes compared · cost $
            {replay.diff.original_cost_usd.toFixed(6)} → ${replay.diff.replay_cost_usd.toFixed(6)}{' '}
            · wall {replay.diff.original_wall_ms}ms → {replay.diff.replay_wall_ms}ms · served from
            the event log, nothing re-executed
          </span>
        </div>
      )}

      {/* ------------------------------------------------------------- main */}
      {mode === 'orchestrator' && (
      <main className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-[1.15fr_1fr] gap-3 p-3">
        <Panel
          title={clarification ? 'Clarify & approve' : 'Agent graph'}
          subtitle={graph ? `${graph.nodes.length} nodes · ${graph.edges.length} edges` : undefined}
          right={graph ? <StatusLegend /> : undefined}
          className="min-h-[340px]"
        >
          {clarification ? (
            <ClarifyPanel
              clarification={clarification}
              onSubmit={compile}
              busy={busy === 'compile'}
            />
          ) : busy === 'compile' ? (
            <Empty icon="⚖" title="The council is deliberating">
              Two models are proposing designs, ranking each other anonymously, and a chairman is
              merging the winner.
            </Empty>
          ) : graph ? (
            <div className="h-full flex flex-col">
              <div className="flex-1 min-h-0">
                <GraphCanvas
                  graph={graph} statuses={statuses} selected={selected} onSelect={setSelected}
                />
              </div>
              {selected && <NodeInspector graph={graph} id={selected} locked={locked} />}
              {council && <CouncilStrip council={council} />}
            </div>
          ) : (
            <Empty icon="◆" title="One goal in, an agent team out">
              Type a goal and press Start. The system asks clarifying questions, requests
              permission, then designs the agent team itself — you never hand-code an agent.
            </Empty>
          )}
        </Panel>

        <Panel
          title={tab === 'trace' ? 'Live trace' : tab === 'metrics' ? 'Honest metrics' : 'Workflow DNA'}
          subtitle={
            tab === 'trace'
              ? runId ? `${events.length} events` : undefined
              : tab === 'metrics'
              ? metrics ? `${metrics.agents.length} agents measured` : undefined
              : runId ? 'predicts, then remembers' : undefined
          }
          className="min-h-[340px]"
          right={
            <div className="flex gap-0.5 p-0.5 rounded-lg bg-[var(--color-surface-2)]">
              {(['trace', 'metrics', 'dna'] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`min-h-8 px-2.5 rounded-md text-[11px] transition-colors ${
                    tab === t
                      ? 'bg-[var(--color-surface-4)] text-[var(--color-ink)]'
                      : 'text-[var(--color-ink-faint)] hover:text-[var(--color-ink-muted)]'
                  }`}
                >
                  {t === 'dna' ? 'workflow dna' : t}
                </button>
              ))}
            </div>
          }
        >
          {tab === 'trace' ? (
            live && events.length === 0
              ? <SkeletonRows n={6} />
              : <TraceViewer events={events} live={live} />
          ) : tab === 'dna' ? (
            <WorkflowDNAPanel events={events} runId={runId} />
          ) : busy === 'marginal' ? (
            <Empty icon="📈" title="Running the same task at depth 1, 2, 3 and 4">
              Each depth is a full run. The report is allowed to conclude that a smaller graph was
              better.
            </Empty>
          ) : (
            <MetricsPanel metrics={metrics} report={report} />
          )}
        </Panel>
      </main>
      )}
    </div>
  )
}

function NodeInspector({ graph, id, locked }: { graph: GraphSpec; id: string; locked: boolean }) {
  const node = graph.nodes.find((n) => n.id === id)
  if (!node) return null
  const incoming = graph.edges.filter((e) => e.to_node === id)

  return (
    <div className="shrink-0 border-t border-[var(--color-line)] bg-[var(--color-surface-2)] p-3 text-[11px] max-h-[200px] overflow-auto">
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        <span className="font-semibold text-[12px]">{node.id}</span>
        <Badge>{node.type}</Badge>
        {locked && <Badge tone="warn">locked</Badge>}
        {node.agent?.allowed_side_effects.length ? (
          <Badge tone="warn">side effects</Badge>
        ) : (
          <Badge tone="good">read-only</Badge>
        )}
      </div>

      {node.agent ? (
        <dl className="grid grid-cols-[104px_1fr] gap-y-1 text-[var(--color-ink-muted)]">
          <dt className="text-[var(--color-ink-faint)]">model</dt>
          <dd className="font-mono">{node.agent.model}</dd>
          <dt className="text-[var(--color-ink-faint)]">fallback</dt>
          <dd className="font-mono">{node.agent.fallback_model ?? '—'}</dd>
          <dt className="text-[var(--color-ink-faint)]">tools</dt>
          <dd className="font-mono">{node.agent.tools.join(', ') || 'none declared'}</dd>
          <dt className="text-[var(--color-ink-faint)]">budget</dt>
          <dd className="font-mono">
            {node.agent.budget_tokens} tokens · {node.agent.timeout_s}s
          </dd>
          <dt className="text-[var(--color-ink-faint)]">contract</dt>
          <dd className="leading-relaxed">{node.agent.system_contract}</dd>
        </dl>
      ) : (
        <p className="text-[var(--color-ink-faint)]">Control node — carries no model call.</p>
      )}

      {incoming.length > 0 && (
        <div className="mt-2 pt-2 border-t border-[var(--color-line)]">
          <div className="text-[var(--color-ink-faint)] mb-1">Incoming handoffs</div>
          {incoming.map((e, i) => (
            <div key={i} className="font-mono text-[10px] text-[var(--color-ink-muted)]">
              {e.from_node} → {e.to_node}{' '}
              {Object.keys(e.handoff_schema ?? {}).length ? (
                <span className="text-[var(--color-done)]">schema enforced</span>
              ) : (
                <span className="text-[var(--color-ink-faint)]">control edge</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function CouncilStrip({ council }: { council: CouncilSummary }) {
  return (
    <div className="shrink-0 border-t border-[var(--color-line)] px-3 py-2 text-[10px] flex items-center gap-2 flex-wrap">
      <span className="text-[var(--color-ink-faint)] uppercase tracking-[0.07em]">Council</span>
      <Badge tone="info">chairman {council.chairman.split(':').pop()}</Badge>
      <Badge>winner {council.winner_label}</Badge>
      <Badge tone={council.escalated ? 'bad' : 'good'}>
        disagreement {council.disagreement}
      </Badge>
      <span className="text-[var(--color-ink-faint)] font-mono">
        {council.proposals.length} proposals · {council.rankings.length} anonymous rankings
      </span>
    </div>
  )
}
