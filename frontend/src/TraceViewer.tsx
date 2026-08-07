import { useEffect, useMemo, useRef, useState } from 'react'
import type { EventType, LogEvent } from './api'
import { Badge, Empty } from './ui'

/** Security events are never a normal log line. */
const ALERT: EventType[] = ['TOOL_BLOCKED', 'INJECTION_BLOCKED']
const BAD: EventType[] = ['HANDOFF_REJECTED', 'BRANCH_FAILED', 'BUDGET_EXCEEDED', 'CANCEL']
const GOOD: EventType[] = ['HANDOFF_VALIDATED', 'NODE_END', 'RUN_END', 'APPROVAL_GRANTED']
const WARN: EventType[] = ['REPAIR_ATTEMPT', 'RETRY', 'COMPENSATE']

function tone(t: EventType) {
  if (ALERT.includes(t)) return 'alert'
  if (BAD.includes(t)) return 'bad'
  if (WARN.includes(t)) return 'warn'
  if (GOOD.includes(t)) return 'good'
  return 'default'
}

function summarise(e: LogEvent): string {
  const p = e.payload ?? {}
  switch (e.event_type) {
    case 'TOOL_BLOCKED':
      return `${p.agent_id ?? e.agent_id} attempted '${p.attempted_tool}' — outside declared capabilities`
    case 'INJECTION_BLOCKED':
      return `payload from '${p.from_node}' quarantined before '${p.into_node}' — ${(p.rules as string[])?.join(', ')}`
    case 'HANDOFF_REJECTED':
      return `schema rejected: ${(p.errors as string[])?.[0] ?? ''}`
    case 'REPAIR_ATTEMPT':
      return `returning validator errors to producer (${p.attempt}/${p.of})`
    case 'HANDOFF_VALIDATED':
      return `contract satisfied on attempt ${p.attempt}`
    case 'BRANCH_FAILED':
      return String(p.reason ?? 'branch failed')
    case 'BUDGET_EXCEEDED':
      return p.reason === 'timeout_s' ? `timeout after ${p.timeout_s}s` : `spent ${p.spent}/${p.budget} tokens`
    case 'RETRY':
      return p.fallback ? `${p.primary} → ${p.fallback}` : `retry ${p.attempt}/${p.of}`
    case 'COUNCIL_VERDICT':
      return `chairman ${p.chairman} · winner ${p.winner_label} (${p.winner_author}) · disagreement ${p.disagreement}`
    case 'COUNCIL_PROPOSAL':
      return `proposal from ${p.model}`
    case 'COUNCIL_RANKING':
      return `anonymous ranking: ${(p.order as string[])?.join(' > ')}`
    case 'RUN_END':
      return `parallel efficiency ${p.parallel_efficiency}× · wall ${p.wall_clock_ms}ms`
    case 'TOOL_RESULT':
      return `${p.model ?? p.tool}${p.replayed ? ' (served from log)' : ''}`
    case 'COMPENSATE':
      return `compensating for '${p.compensates}'`
    default:
      return ''
  }
}

export function TraceViewer({ events, live }: { events: LogEvent[]; live: boolean }) {
  const [filter, setFilter] = useState<'all' | 'security' | 'contract'>('all')
  const [stick, setStick] = useState(true)
  const endRef = useRef<HTMLDivElement>(null)

  const shown = useMemo(() => {
    if (filter === 'security') return events.filter((e) => ALERT.includes(e.event_type))
    if (filter === 'contract')
      return events.filter((e) =>
        ['HANDOFF_EMITTED', 'HANDOFF_VALIDATED', 'HANDOFF_REJECTED', 'REPAIR_ATTEMPT'].includes(e.event_type))
    return events
  }, [events, filter])

  useEffect(() => {
    if (stick && live) endRef.current?.scrollIntoView({ block: 'end' })
  }, [shown.length, stick, live])

  const alerts = events.filter((e) => ALERT.includes(e.event_type)).length

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex items-center gap-1.5 px-3 py-2 border-b border-[var(--color-line)] shrink-0">
        {(['all', 'contract', 'security'] as const).map((f) => (
          <button
            key={f} onClick={() => setFilter(f)}
            className={`min-h-8 px-2.5 rounded text-[11px] transition-colors ${
              filter === f
                ? 'bg-[var(--color-surface-3)] text-[var(--color-ink)]'
                : 'text-[var(--color-ink-faint)] hover:text-[var(--color-ink-muted)]'
            }`}
          >
            {f}
            {f === 'security' && alerts > 0 && (
              <span className="ml-1 text-[var(--color-failed)] font-semibold">{alerts}</span>
            )}
          </button>
        ))}
        <label className="ml-auto flex items-center gap-1.5 text-[11px] text-[var(--color-ink-faint)] cursor-pointer">
          <input type="checkbox" checked={stick} onChange={(e) => setStick(e.target.checked)} className="accent-[var(--color-accent)]" />
          follow
        </label>
      </div>

      <div className="flex-1 min-h-0 overflow-auto font-mono text-[11px]">
        {shown.length === 0 && <Empty>No events yet.</Empty>}
        {shown.map((e) => {
          const t = tone(e.event_type)
          return (
            <div
              key={e.seq}
              className={`flex gap-2 px-3 py-1.5 border-b border-[var(--color-surface-2)] ${
                t === 'alert' ? 'bg-[#f8717114] border-l-2 border-l-[var(--color-failed)]' : ''
              }`}
            >
              <span className="text-[var(--color-ink-faint)] tabular-nums w-8 shrink-0">{e.seq}</span>
              <span className="shrink-0 w-[150px]">
                <Badge tone={t === 'alert' ? 'bad' : t === 'bad' ? 'bad' : t === 'warn' ? 'warn' : t === 'good' ? 'good' : 'default'}>
                  {e.event_type}
                </Badge>
              </span>
              <span className="text-[var(--color-ink-muted)] w-[86px] shrink-0 truncate">{e.node_id ?? '—'}</span>
              <span className={`flex-1 min-w-0 break-words ${t === 'alert' ? 'text-[var(--color-failed)] font-semibold' : 'text-[var(--color-ink-muted)]'}`}>
                {summarise(e)}
              </span>
              {e.latency_ms != null && (
                <span className="text-[var(--color-ink-faint)] tabular-nums shrink-0">{e.latency_ms}ms</span>
              )}
            </div>
          )
        })}
        <div ref={endRef} />
      </div>
    </div>
  )
}
