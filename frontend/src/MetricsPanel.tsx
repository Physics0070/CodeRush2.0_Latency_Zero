import { useEffect, useState } from 'react'
import {
  Bar, BarChart, CartesianGrid, Cell, LabelList, Legend, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { api, type MarginalValueReport, type ModelProfile, type RunMetrics } from './api'
import { Empty, Stat } from './ui'

const AXIS = { stroke: '#71717a', fontSize: 10 }
const GRID = '#232329'

const tooltipStyle = {
  background: '#1a1a1e',
  border: '1px solid #2e2e36',
  borderRadius: 8,
  fontSize: 11,
  color: '#f4f4f5',
}

/** MIG below this means the agent added nothing the others did not have. */
const MIG_FLOOR = 0.1

/** Must match DUPLICATE_THRESHOLD in backend/metrics/metrics.py. */
const DUPLICATE_THRESHOLD = 0.75

// Categorical, dark-mode, slots 1-3 of the validated default palette (blue,
// orange, aqua) - validated against this app's own chart surface (#1a1a1e):
// worst adjacent CVD ΔE 9.4 (deutan), normal-vision ΔE 26.5, all >=3:1 contrast.
const MODEL_COLORS = ['#3987e5', '#d95926', '#199e70']

const AXIS_LABEL: Record<keyof ModelProfile, string> = {
  reasoning_logic: 'Reasoning', task_proficiency: 'Task fit', context_relation: 'Context',
  agentic_capability: 'Agentic', factuality_safety: 'Safety', real_world_data: 'Real-world',
}

function shortModelName(m: string): string {
  return m.replace(/^(ollama|groq|gemini):/, '')
}

function migColor(v: number) {
  if (v < MIG_FLOOR) return 'var(--color-failed)'
  if (v < 0.3) return '#fbbf24'
  return 'var(--color-done)'
}

export function MetricsPanel({ metrics, report }: {
  metrics: RunMetrics | null
  report: MarginalValueReport | null
}) {
  const [catalog, setCatalog] = useState<{ model: string; profile: ModelProfile }[]>([])

  useEffect(() => {
    api.modelCatalog().then((r) => setCatalog(r.models)).catch(() => {})
  }, [])

  if (!metrics) return <Empty>Run the graph, then metrics appear here.</Empty>

  const migData = metrics.agents.map((a) => ({
    node: a.node_id,
    mig: a.marginal_information_gain,
    findings: a.findings,
  }))

  return (
    <div className="p-3 space-y-4">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
        <Stat label="Parallel eff." value={`${metrics.parallel_efficiency}×`}
              tone={metrics.parallel_efficiency > 2 ? 'good' : 'default'}
              hint="Sum of node latencies ÷ wall clock" />
        <Stat label="Handoff validity"
              value={metrics.handoff_validity == null ? '—' : `${Math.round(metrics.handoff_validity * 100)}%`}
              tone={metrics.handoff_validity === 1 ? 'good' : 'warn'}
              hint="validated ÷ (validated + rejected)" />
        <Stat label="Redundancy" value={metrics.redundancy_index}
              tone={metrics.redundancy_index > 0.7 ? 'bad' : 'default'}
              hint="Mean pairwise cosine across branches" />
        <Stat label="Duplicate work" value={metrics.duplicate_work}
              tone={metrics.duplicate_work > 0 ? 'bad' : 'good'}
              hint={`Branch pairs with cosine > ${DUPLICATE_THRESHOLD}`} />
        <Stat label="Unique findings" value={metrics.unique_findings} />
        <Stat label="Tokens" value={metrics.total_tokens.toLocaleString()} />
        <Stat label="Cost" value={`$${metrics.total_cost_usd.toFixed(6)}`}
              hint="Local models are genuinely $0" />
        <Stat label="Recovery"
              value={metrics.recovery_rate == null ? '—' : `${Math.round(metrics.recovery_rate * 100)}%`}
              tone={metrics.recovery_rate ? 'good' : 'default'}
              hint="Compensated ÷ failed branches" />
      </div>

      {/* The honest verdict, stated plainly rather than buried in a chart. */}
      {metrics.verdict && (
        <div
          className="rounded-lg border px-3 py-2.5 text-[12px] leading-relaxed"
          style={{
            borderColor: metrics.duplicate_work || (metrics.least_valuable_agent &&
              migData.some((d) => d.mig < MIG_FLOOR)) ? 'var(--color-failed)' : 'var(--color-line)',
            background: 'var(--color-surface-2)',
          }}
        >
          <div className="text-[10px] uppercase tracking-wider text-[var(--color-ink-faint)] mb-1">
            System verdict
          </div>
          <p className="text-[var(--color-ink)]">{metrics.verdict}</p>
        </div>
      )}

      <div>
        <h3 className="text-[11px] uppercase tracking-wider text-[var(--color-ink-faint)] mb-2">
          Marginal information gain
        </h3>
        <div className="h-[150px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={migData} margin={{ top: 4, right: 8, bottom: 4, left: -22 }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="node" tick={AXIS} axisLine={false} tickLine={false} />
              <YAxis domain={[0, 1]} tick={AXIS} axisLine={false} tickLine={false} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: '#ffffff08' }} />
              <Bar dataKey="mig" radius={[3, 3, 0, 0]}>
                {migData.map((d) => <Cell key={d.node} fill={migColor(d.mig)} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
        <p className="text-[10px] text-[var(--color-ink-faint)] mt-1">
          1 − max cosine similarity to any other agent. Red is below {MIG_FLOOR}: that agent
          produced almost nothing the others did not already have.
        </p>
      </div>

      {metrics.agents.length > 1 && (
        <div>
          <h3 className="text-[11px] uppercase tracking-wider text-[var(--color-ink-faint)] mb-2">
            Branch redundancy
          </h3>
          <RedundancyHeatmap metrics={metrics} />
        </div>
      )}

      {report && report.points.length > 0 && (
        <div>
          <h3 className="text-[11px] uppercase tracking-wider text-[var(--color-ink-faint)] mb-2">
            Marginal value by graph size
          </h3>
          <div className="h-[150px]">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={report.points} margin={{ top: 4, right: 8, bottom: 4, left: -22 }}>
                <CartesianGrid stroke={GRID} />
                <XAxis dataKey="depth" tick={AXIS} axisLine={false} tickLine={false}
                       label={{ value: 'agents', position: 'insideBottom', offset: -2, fill: '#71717a', fontSize: 10 }} />
                <YAxis tick={AXIS} axisLine={false} tickLine={false} />
                <Tooltip contentStyle={tooltipStyle} />
                <Line type="monotone" dataKey="quality_per_1k_tokens"
                      stroke="var(--color-accent)" strokeWidth={2}
                      dot={{ r: 3, fill: 'var(--color-accent)' }} name="findings / 1k tokens" />
                <Line type="monotone" dataKey="unique_findings"
                      stroke="var(--color-running)" strokeWidth={1.5} strokeDasharray="4 3"
                      dot={{ r: 2 }} name="unique findings" />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <p className="text-[11px] text-[var(--color-ink)] mt-1.5 leading-relaxed">
            {report.recommendation}
          </p>
        </div>
      )}

      {catalog.length > 0 && (
        <div>
          <h3 className="text-[11px] uppercase tracking-wider text-[var(--color-ink-faint)] mb-2">
            Model routing axes
          </h3>
          <div className="h-[190px]">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={(Object.keys(AXIS_LABEL) as (keyof ModelProfile)[]).map((axis) => {
                  const row: Record<string, string | number> = { axis: AXIS_LABEL[axis] }
                  catalog.forEach((c) => { row[shortModelName(c.model)] = c.profile[axis] })
                  return row
                })}
                margin={{ top: 10, right: 8, bottom: 4, left: -22 }}
              >
                <CartesianGrid stroke={GRID} vertical={false} />
                <XAxis dataKey="axis" tick={AXIS} axisLine={false} tickLine={false} />
                <YAxis domain={[0, 1]} tick={AXIS} axisLine={false} tickLine={false} />
                <Tooltip contentStyle={tooltipStyle} />
                <Legend wrapperStyle={{ fontSize: 10, color: '#a1a1aa' }} />
                {catalog.map((c, i) => (
                  <Bar
                    key={c.model} dataKey={shortModelName(c.model)}
                    fill={MODEL_COLORS[i % MODEL_COLORS.length]} radius={[3, 3, 0, 0]}
                  >
                    <LabelList
                      dataKey={shortModelName(c.model)} position="top" fontSize={8} fill="#71717a"
                      formatter={(v: unknown) => (typeof v === 'number' ? v.toFixed(2) : '')}
                    />
                  </Bar>
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
          <p className="text-[11px] text-[var(--color-ink-faint)] mt-1.5 leading-relaxed">
            Curated per-model estimates, not live-measured — see backend/providers/catalog.py.
          </p>
        </div>
      )}
    </div>
  )
}

function RedundancyHeatmap({ metrics }: { metrics: RunMetrics }) {
  const nodes = metrics.agents.map((a) => a.node_id)
  const lookup = new Map(metrics.duplicate_pairs.map((p) => [`${p.a}|${p.b}`, p.cosine]))
  const value = (a: string, b: string) =>
    a === b ? 1 : lookup.get(`${a}|${b}`) ?? lookup.get(`${b}|${a}`) ?? null

  return (
    <div className="overflow-x-auto">
      <table className="text-[10px] border-separate border-spacing-0.5">
        <thead>
          <tr>
            <th />
            {nodes.map((n) => (
              <th key={n} className="px-1 py-0.5 font-normal text-[var(--color-ink-faint)] text-left">{n}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {nodes.map((a) => (
            <tr key={a}>
              <td className="pr-2 text-[var(--color-ink-faint)] whitespace-nowrap">{a}</td>
              {nodes.map((b) => {
                const v = value(a, b)
                const dup = v != null && v > DUPLICATE_THRESHOLD && a !== b
                return (
                  <td key={b}
                      title={v == null ? 'below duplicate threshold' : `cosine ${v.toFixed(3)}`}
                      className="h-7 w-14 text-center rounded tabular-nums"
                      style={{
                        background: a === b ? 'var(--color-surface-3)'
                          : dup ? 'rgba(248,113,113,0.28)' : 'var(--color-surface-2)',
                        color: dup ? 'var(--color-failed)' : 'var(--color-ink-faint)',
                      }}>
                    {a === b ? '—' : v == null ? '·' : v.toFixed(2)}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-[10px] text-[var(--color-ink-faint)] mt-1">
        Only pairs above the {DUPLICATE_THRESHOLD} duplicate threshold are scored; “·” is below it.
      </p>
    </div>
  )
}
