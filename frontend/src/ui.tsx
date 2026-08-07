import type { ReactNode } from 'react'

/** Shared primitives. Every interactive target is at least 44px tall. */

export function Panel({ title, subtitle, right, children, className = '' }: {
  title: string; subtitle?: string; right?: ReactNode; children: ReactNode; className?: string
}) {
  return (
    <section
      className={`flex flex-col min-h-0 rounded-[var(--radius-panel)] border border-[var(--color-line)] bg-[var(--color-surface-1)]/80 backdrop-blur-sm shadow-[0_1px_0_0_#ffffff08_inset,0_8px_28px_-12px_#000a] ${className}`}
    >
      <header className="flex items-center justify-between gap-3 px-4 py-3 border-b border-[var(--color-line)] shrink-0">
        <div className="min-w-0">
          <h2 className="text-[12px] font-semibold tracking-[0.08em] uppercase text-[var(--color-ink-muted)]">
            {title}
          </h2>
          {subtitle && (
            <p className="text-[11px] text-[var(--color-ink-faint)] truncate mt-0.5">{subtitle}</p>
          )}
        </div>
        {right}
      </header>
      <div className="flex-1 min-h-0 overflow-auto">{children}</div>
    </section>
  )
}

const BUTTON_VARIANTS = {
  default:
    'bg-[var(--color-surface-3)] text-[var(--color-ink)] hover:bg-[var(--color-surface-4)] border border-[var(--color-line-strong)]',
  primary:
    'bg-[var(--color-accent)] text-[#032420] font-semibold hover:bg-[var(--color-accent-dim)] border border-transparent shadow-[0_2px_12px_-4px_var(--color-accent-deep)]',
  danger:
    'bg-[#f8717119] text-[var(--color-failed)] font-semibold hover:bg-[#f8717129] border border-[#f8717155]',
  ghost:
    'bg-transparent text-[var(--color-ink-muted)] hover:text-[var(--color-ink)] hover:bg-[var(--color-surface-2)] border border-transparent',
} as const

export function Button({
  children, onClick, disabled, variant = 'default', title, loading, type = 'button',
}: {
  children: ReactNode
  onClick?: () => void
  disabled?: boolean
  variant?: keyof typeof BUTTON_VARIANTS
  title?: string
  loading?: boolean
  type?: 'button' | 'submit'
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled || loading}
      title={title}
      aria-busy={loading || undefined}
      className={`inline-flex items-center gap-2 min-h-11 px-4 rounded-[10px] text-[13px] transition-all duration-150 disabled:opacity-40 disabled:cursor-not-allowed active:scale-[0.98] ${BUTTON_VARIANTS[variant]}`}
    >
      {loading && <Spinner />}
      {children}
    </button>
  )
}

export function Spinner({ className = '' }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={`inline-block h-3.5 w-3.5 rounded-full border-2 border-current border-r-transparent animate-spin ${className}`}
    />
  )
}

const STAT_TONES = {
  default: 'text-[var(--color-ink)]',
  good: 'text-[var(--color-done)]',
  warn: 'text-[var(--color-warn)]',
  bad: 'text-[var(--color-failed)]',
} as const

export function Stat({ label, value, tone = 'default', hint }: {
  label: string; value: ReactNode; tone?: keyof typeof STAT_TONES; hint?: string
}) {
  return (
    <div
      className="rounded-[10px] border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2.5 transition-colors hover:border-[var(--color-line-strong)]"
      title={hint}
    >
      <div className="text-[10px] uppercase tracking-[0.07em] text-[var(--color-ink-faint)]">
        {label}
      </div>
      <div className={`text-[17px] font-semibold tabular-nums mt-0.5 ${STAT_TONES[tone]}`}>
        {value}
      </div>
    </div>
  )
}

const BADGE_TONES = {
  default: 'bg-[var(--color-surface-3)] text-[var(--color-ink-muted)]',
  good: 'bg-[#34d3991f] text-[var(--color-done)]',
  bad: 'bg-[#f871711f] text-[var(--color-failed)]',
  warn: 'bg-[#fbbf241f] text-[var(--color-warn)]',
  info: 'bg-[#60a5fa1f] text-[var(--color-running)]',
  accent: 'bg-[#5eead41f] text-[var(--color-accent)]',
} as const

export function Badge({ children, tone = 'default', mono = true }: {
  children: ReactNode; tone?: keyof typeof BADGE_TONES; mono?: boolean
}) {
  return (
    <span
      className={`inline-flex items-center rounded-md px-1.5 py-0.5 text-[10px] font-medium whitespace-nowrap ${
        mono ? 'font-mono' : ''
      } ${BADGE_TONES[tone]}`}
    >
      {children}
    </span>
  )
}

export function Empty({ icon, title, children }: {
  icon?: ReactNode; title?: string; children: ReactNode
}) {
  return (
    <div className="h-full grid place-items-center p-8">
      <div className="max-w-sm text-center">
        {icon && <div className="text-2xl mb-3 opacity-40" aria-hidden>{icon}</div>}
        {title && (
          <p className="text-[14px] font-medium text-[var(--color-ink-muted)] mb-1.5">{title}</p>
        )}
        <p className="text-[12px] leading-relaxed text-[var(--color-ink-faint)]">{children}</p>
      </div>
    </div>
  )
}

/** Where the user is in the goal → graph → run → measure flow. */
export function StepRail({ current }: { current: number }) {
  const steps = ['Goal', 'Clarify', 'Graph', 'Run', 'Measure']
  return (
    <ol className="flex items-center gap-1.5" aria-label="Progress">
      {steps.map((s, i) => {
        const state = i < current ? 'done' : i === current ? 'active' : 'todo'
        return (
          <li key={s} className="flex items-center gap-1.5">
            <span
              aria-current={state === 'active' ? 'step' : undefined}
              className={`text-[10px] px-2 py-1 rounded-md transition-colors ${
                state === 'done'
                  ? 'text-[var(--color-done)] bg-[#34d39914]'
                  : state === 'active'
                    ? 'text-[var(--color-accent)] bg-[#5eead418] font-semibold'
                    : 'text-[var(--color-ink-faint)]'
              }`}
            >
              {state === 'done' ? '✓ ' : ''}{s}
            </span>
            {i < steps.length - 1 && (
              <span aria-hidden className="w-3 h-px bg-[var(--color-line-strong)]" />
            )}
          </li>
        )
      })}
    </ol>
  )
}

export function SkeletonRows({ n = 5 }: { n?: number }) {
  return (
    <div className="p-3 space-y-2" aria-hidden>
      {Array.from({ length: n }).map((_, i) => (
        <div key={i} className="skeleton h-7 rounded-md" style={{ opacity: 1 - i * 0.14 }} />
      ))}
    </div>
  )
}
