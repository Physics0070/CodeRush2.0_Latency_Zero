import { useState } from 'react'
import { Button } from './ui'

export interface Clarification {
  questions: { id: string; text: string }[]
  permission: string
  plan: {
    intent: string; complexity: string; rationale: string; source: string
    specialists: { id: string; role: string }[]
  }
}

/**
 * Demo step 2. Human input in this whole system is three things: the goal,
 * these answers, and one approve click. Everything after is automatic.
 */
export function ClarifyPanel({ clarification, onSubmit, busy }: {
  clarification: Clarification
  onSubmit: (answers: Record<string, string>) => void
  busy: boolean
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [granted, setGranted] = useState(false)

  const { plan } = clarification

  return (
    <div className="p-4 space-y-4">
      {plan?.rationale && (
        <p className="text-[11px] leading-relaxed text-[var(--color-ink-faint)]">
          {plan.rationale}
        </p>
      )}
      <div className="space-y-3">
        {clarification.questions.map((q, i) => (
          <div key={q.id}>
            <label htmlFor={q.id} className="block text-[12px] text-[var(--color-ink-muted)] mb-1.5">
              <span className="text-[var(--color-ink-faint)] mr-1.5">{i + 1}.</span>
              {q.text}
            </label>
            <input
              id={q.id}
              value={answers[q.text] ?? ''}
              onChange={(e) => setAnswers({ ...answers, [q.text]: e.target.value })}
              placeholder="Answering helps tailor the plan"
              className="w-full min-h-11 px-3 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-line)] text-[13px] placeholder:text-[var(--color-ink-faint)] focus:border-[var(--color-accent)] outline-none"
            />
          </div>
        ))}
      </div>

      <div className="rounded-lg border border-[var(--color-line-strong)] bg-[var(--color-surface-2)] p-3">
        <div className="text-[10px] uppercase tracking-wider text-[var(--color-ink-faint)] mb-1.5">
          Permission requested
        </div>
        <p className="text-[12px] leading-relaxed text-[var(--color-ink)]">{clarification.permission}</p>
        <label className="mt-3 flex items-center gap-2 text-[12px] cursor-pointer select-none">
          <input
            type="checkbox" checked={granted}
            onChange={(e) => setGranted(e.target.checked)}
            className="h-4 w-4 accent-[var(--color-accent)]"
          />
          I approve this plan
        </label>
      </div>

      <Button variant="primary" disabled={!granted || busy} onClick={() => onSubmit(answers)}>
        {busy ? 'Council is designing the graph…' : 'Compile agent graph'}
      </Button>
    </div>
  )
}
