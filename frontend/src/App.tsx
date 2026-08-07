import { useEffect, useState } from 'react'

type Health = { status: string; version: string }

/**
 * Block 0 scaffold. Its only job is to prove the frontend build is wired to the
 * FastAPI backend on the same origin. The real surface - GraphCanvas,
 * TraceViewer, MetricsPanel, ClarifyPanel - lands in block 10, after the
 * execution contract is solid.
 */
export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetch('/health')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setHealth)
      .catch((e: Error) => setError(e.message))
  }, [])

  return (
    <main className="min-h-screen flex items-center justify-center p-8">
      <div className="max-w-xl w-full space-y-4">
        <h1 className="text-2xl font-semibold tracking-tight">Agent Council Orchestrator</h1>
        <p className="text-neutral-400 text-sm">
          Unified Agent Form Orchestrator &middot; AE-03 &middot; Latency Zero
        </p>
        <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-4 font-mono text-sm">
          {error && <span className="text-red-400">backend unreachable: {error}</span>}
          {!error && !health && <span className="text-neutral-500">checking backend&hellip;</span>}
          {health && (
            <span className="text-emerald-400">
              backend {health.status} &middot; v{health.version}
            </span>
          )}
        </div>
      </div>
    </main>
  )
}
