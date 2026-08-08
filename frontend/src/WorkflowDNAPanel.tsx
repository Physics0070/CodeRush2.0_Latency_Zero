import { useEffect, useState } from 'react'
import type { DnaPredictedPayload, DnaRecordedPayload, LogEvent, WorkflowDnaExecution } from './api'
import { api } from './api'
import { Badge, Empty, Stat } from './ui'

/** Same three-band read as the rest of this app's Stat/Badge tones. */
function fitnessTone(f: number): 'good' | 'warn' | 'bad' {
  if (f >= 70) return 'good'
  if (f >= 50) return 'warn'
  return 'bad'
}

export function WorkflowDNAPanel({ events, runId }: { events: LogEvent[]; runId: string | null }) {
  const [history, setHistory] = useState<WorkflowDnaExecution[]>([])
  const [historyError, setHistoryError] = useState<string | null>(null)

  // Re-fetch whenever the run changes (including going from a run in progress
  // to finished, since that's when a new row lands in Workflow DNA's history).
  useEffect(() => {
    api.workflowDnaHistory(20).then((r) => setHistory(r.executions)).catch((e) => setHistoryError((e as Error).message))
  }, [runId, events.length])

  if (!runId) {
    return (
      <Empty icon="🧬" title="No prediction yet">
        Workflow DNA predicts a fitness score before a run starts, and records the real
        outcome once it finishes. This panel fills in as soon as a run begins.
      </Empty>
    )
  }

  const predicted = events.find((e) => e.event_type === 'DNA_PREDICTED')
  const recorded = events.find((e) => e.event_type === 'DNA_RECORDED')
  const pred = predicted?.payload as DnaPredictedPayload | undefined
  const rec = recorded?.payload as DnaRecordedPayload | undefined

  return (
    <div className="p-3 space-y-4">
      {!pred && (
        <p className="text-[12px] text-[var(--color-ink-faint)]">
          Waiting for Workflow DNA's pre-execution prediction…
        </p>
      )}

      {pred && (
        <div>
          <h3 className="text-[11px] uppercase tracking-wider text-[var(--color-ink-faint)] mb-2">
            Generation 1
          </h3>
          <div className="grid grid-cols-2 gap-2">
            <Stat
              label="Predicted fitness"
              value={pred.predicted_fitness}
              tone={fitnessTone(pred.predicted_fitness)}
            />
            <Stat
              label="Estimate source"
              value={pred.estimate_source === 'history' ? 'History' : 'Cold start'}
              hint={
                pred.estimate_source === 'history'
                  ? `${pred.similar_workflow_count} similar past workflows`
                  : 'No similar history yet — using the pretraining prior'
              }
            />
          </div>

          {pred.mutation_suggestion && (
            <div className="mt-3 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2.5">
              <div className="flex items-center gap-2 mb-2">
                <Badge tone="info">Mutation applied</Badge>
                <span className="text-[12px] text-[var(--color-ink)] font-mono">
                  {pred.mutation_suggestion.mutation_type}
                </span>
              </div>
              <div className="text-[10px] uppercase tracking-wider text-[var(--color-ink-faint)] mb-2">
                Generation 2
              </div>
              <div className="grid grid-cols-2 gap-2">
                <Stat
                  label="Predicted fitness"
                  value={pred.mutation_suggestion.predicted_fitness}
                  tone={fitnessTone(pred.mutation_suggestion.predicted_fitness)}
                />
                <Stat
                  label="Selected"
                  value={
                    pred.mutation_suggestion.predicted_fitness > pred.predicted_fitness
                      ? 'Generation 2'
                      : 'Generation 1'
                  }
                />
              </div>
              <p className="text-[10px] text-[var(--color-ink-faint)] mt-2 leading-relaxed">
                {pred.mutation_suggestion.note}
              </p>
            </div>
          )}
        </div>
      )}

      {rec && (
        <div>
          <h3 className="text-[11px] uppercase tracking-wider text-[var(--color-ink-faint)] mb-2">
            Actual outcome
          </h3>
          <div className="grid grid-cols-2 lg:grid-cols-3 gap-2">
            <Stat label="Fitness" value={rec.fitness} tone={fitnessTone(rec.fitness)} />
            <Stat label="Quality" value={rec.quality.toFixed(2)} />
            {rec.predicted_fitness != null && (
              <Stat
                label="Predicted → actual"
                value={`${rec.predicted_fitness} → ${rec.fitness}`}
                tone={Math.abs(rec.fitness - rec.predicted_fitness) < 10 ? 'good' : 'warn'}
                hint="How close the pre-execution prediction landed"
              />
            )}
          </div>
        </div>
      )}

      <div>
        <h3 className="text-[11px] uppercase tracking-wider text-[var(--color-ink-faint)] mb-2">
          Evolution history
        </h3>
        {historyError ? (
          <p className="text-[11px] text-[var(--color-failed)]">{historyError}</p>
        ) : history.length === 0 ? (
          <p className="text-[11px] text-[var(--color-ink-faint)]">
            No completed Workflow DNA executions recorded yet — this fills in as runs finish.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] border-separate border-spacing-y-1">
              <thead>
                <tr className="text-[10px] uppercase tracking-wider text-[var(--color-ink-faint)]">
                  <th className="text-left font-normal pr-3">Workflow</th>
                  <th className="text-left font-normal pr-3">Gen</th>
                  <th className="text-left font-normal pr-3">Mutation</th>
                  <th className="text-left font-normal pr-3">Fitness</th>
                  <th className="text-left font-normal">When</th>
                </tr>
              </thead>
              <tbody>
                {history.map((h) => (
                  <tr key={h.workflow_id} className="bg-[var(--color-surface-2)]">
                    <td
                      className="px-2 py-1.5 rounded-l-lg font-mono truncate max-w-[140px]"
                      title={h.workflow_id}
                    >
                      {h.workflow_id}
                    </td>
                    <td className="px-2 py-1.5">{h.generation}</td>
                    <td className="px-2 py-1.5 font-mono text-[var(--color-ink-faint)]">
                      {h.mutation_type}
                    </td>
                    <td className="px-2 py-1.5">
                      {h.fitness != null && <Badge tone={fitnessTone(h.fitness)}>{h.fitness}</Badge>}
                    </td>
                    <td className="px-2 py-1.5 rounded-r-lg text-[var(--color-ink-faint)]">
                      {new Date(h.timestamp).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
