import type { ReactNode } from 'react'

/** Shared primitives. Every interactive target is >= 44px tall. */

export function Panel({ title, right, children, className = '' }: {
  title: string; right?: ReactNode; children: ReactNode; className?: string
}) {
  return (
    <section className={`flex flex-col min-h-0 rounded-xl border border-[var(--color-line)] bg-[var(--color-surface-1)] ${className}`}>
      <header className="flex items-center justify-between gap-3 px-4 py-3 border-b border-[var(--color-line)] shrink-0">
        <h2 className="text-[13px] font-semibold tracking-wide uppercase text-[var(--color-ink-muted)]">
          {title}
        </h2>
        {right}
      </header>
      <div className="flex-1 min-h-0 overflow-auto">{children}</div>
    </section>
  )
}

export function Button({ children, onClick, disabled, variant = 'default', title }: {
  children: ReactNode; onClick?: () => void; disabled?: boolean
  variant?: 'default' | 'primary' | 'danger'; title?: string
}) {
  const styles = {
    default: 'bg-[var(--color-surface-3)] text-[var(--color-ink)] hover:bg-[var(--color-line-strong)]',
    primary: 'bg-[var(--color-accent)] text-[#04120c] font-semibold hover:bg-[var(--color-accent-dim)]',
    danger: 'bg-[var(--color-failed)] text-[#20090b] font-semibold hover:opacity-90',
  }[variant]
  return (
    <button
      type="button" onClick={onClick} disabled={disabled} title={title}
      className={`min-h-11 px-4 rounded-lg text-sm transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${styles}`}
    >
      {children}
    </button>
  )
}

export function Stat({ label, value, tone = 'default', hint }: {
  label: string; value: ReactNode; tone?: 'default' | 'good' | 'warn' | 'bad'; hint?: string
}) {
  const color = {
    default: 'text-[var(--color-ink)]',
    good: 'text-[var(--color-done)]',
    warn: 'text-[#fbbf24]',
    bad: 'text-[var(--color-failed)]',
  }[tone]
  return (
    <div className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2.5" title={hint}>
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-ink-faint)]">{label}</div>
      <div className={`text-lg font-semibold tabular-nums ${color}`}>{value}</div>
    </div>
  )
}

export function Badge({ children, tone = 'default' }: {
  children: ReactNode; tone?: 'default' | 'good' | 'bad' | 'warn' | 'info'
}) {
  const styles = {
    default: 'bg-[var(--color-surface-3)] text-[var(--color-ink-muted)]',
    good: 'bg-[#34d39926] text-[var(--color-done)]',
    bad: 'bg-[#f8717126] text-[var(--color-failed)]',
    warn: 'bg-[#fbbf2426] text-[#fbbf24]',
    info: 'bg-[#60a5fa26] text-[var(--color-running)]',
  }[tone]
  return (
    <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-mono font-medium ${styles}`}>
      {children}
    </span>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="h-full grid place-items-center p-8 text-center text-sm text-[var(--color-ink-faint)]">
      {children}
    </div>
  )
}
