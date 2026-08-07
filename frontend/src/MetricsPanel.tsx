import {
  Bar, BarChart, CartesianGrid, Cell, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import type { MarginalValueReport, RunMetrics } from './api'
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

function migColor(v: number) {
  if (v < MIG_FLOOR) return 'var(--color-failed)'
  if (v < 0.3) return '#fbbf24'
  return 'var(--color-done)'
}

export function MetricsPanel({ metrics, report }: {
  metrics: RunMetrics | null
  report: MarginalValueReport | null
}) {
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
              hint="Branch pairs with cosine > 0.85" />
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
                const dup = v != null && v > 0.85 && a !== b
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
        Only pairs above the 0.85 duplicate threshold are scored; “·” is below it.
      </p>
    </div>
  )
}
