import { useMemo } from 'react'
import ReactFlow, {
  Background, BackgroundVariant, Controls, Handle, Position,
  type Edge, type Node, type NodeProps,
} from 'reactflow'
import type { GraphSpec, GraphNode } from './api'
import { Badge } from './ui'

export type NodeStatus = 'pending' | 'running' | 'done' | 'failed' | 'blocked' | 'skipped'

const STATUS_COLOR: Record<NodeStatus, string> = {
  pending: 'var(--color-pending)',
  running: 'var(--color-running)',
  done: 'var(--color-done)',
  failed: 'var(--color-failed)',
  blocked: 'var(--color-blocked)',
  skipped: 'var(--color-skipped)',
}

const TYPE_GLYPH: Record<string, string> = {
  clarify: '?', approval: '✓', fanout: '⋔', join: '⋈', agent: '◆',
  verify: '⊙', council: '⚖', compensate: '↺', conditional: '◇', subgraph: '▣',
}

interface Data {
  node: GraphNode
  status: NodeStatus
  onSelect: (id: string) => void
  selected: boolean
}

function AcoNode({ data }: NodeProps<Data>) {
  const { node, status, onSelect, selected } = data
  const color = STATUS_COLOR[status]
  const isAgent = node.type === 'agent' || node.type === 'verify'

  return (
    <button
      onClick={() => onSelect(node.id)}
      aria-label={`${node.type} node ${node.id}, ${status}`}
      className={`min-w-[168px] text-left rounded-lg border bg-[var(--color-surface-2)] px-3 py-2 transition-colors ${
        status === 'running' ? 'is-running' : ''
      }`}
      style={{
        borderColor: selected ? 'var(--color-accent)' : color,
        borderWidth: selected ? 2 : 1,
      }}
    >
      <Handle type="target" position={Position.Top} />
      <div className="flex items-center gap-2">
        <span aria-hidden className="text-xs" style={{ color }}>{TYPE_GLYPH[node.type] ?? '•'}</span>
        <span className="text-[13px] font-medium text-[var(--color-ink)] truncate">{node.id}</span>
        <span
          aria-hidden
          className="ml-auto h-2 w-2 rounded-full shrink-0"
          style={{ background: color }}
        />
      </div>
      <div className="mt-1 flex items-center gap-1.5 flex-wrap">
        <Badge>{node.type}</Badge>
        {isAgent && node.agent && (
          <span className="text-[10px] font-mono text-[var(--color-ink-faint)] truncate max-w-[130px]">
            {node.agent.model.replace('ollama:', '').replace('groq:', '')}
          </span>
        )}
        {node.agent && node.agent.allowed_side_effects.length > 0 && (
          <Badge tone="warn">side effects</Badge>
        )}
      </div>
      <Handle type="source" position={Position.Bottom} />
    </button>
  )
}

const nodeTypes = { aco: AcoNode }

/** Only nodes/edges are read - a chat turn's graph carries no config_hash or
 * locked state (never approved, never re-run in place), so callers pass a
 * plain {nodes, edges} shape rather than a fake full GraphSpec. */
type Drawable = Pick<GraphSpec, 'nodes' | 'edges'>

/** Lays the graph out by dependency level, so the parallel row reads as parallel. */
function layout(graph: Drawable): Record<string, { x: number; y: number }> {
  const parents = (id: string) => graph.edges.filter((e) => e.to_node === id).map((e) => e.from_node)
  const compensators = new Set(graph.nodes.filter((n) => n.type === 'compensate').map((n) => n.id))

  const level: Record<string, number> = {}
  const resolve = (id: string, seen = new Set<string>()): number => {
    if (level[id] !== undefined) return level[id]
    if (seen.has(id)) return 0
    seen.add(id)
    const ps = parents(id).filter((p) => !compensators.has(p))
    const v = ps.length === 0 ? 0 : Math.max(...ps.map((p) => resolve(p, seen))) + 1
    level[id] = v
    return v
  }
  graph.nodes.forEach((n) => resolve(n.id))

  // Compensate nodes ride alongside the branch they compensate rather than
  // taking their own slot in the row - counting them as siblings in the
  // spacing math is what crowded a 4-branch graph (8 boxes fighting for one
  // row) into an overlapping mess.
  const byLevel: Record<number, string[]> = {}
  graph.nodes.forEach((n) => {
    if (compensators.has(n.id)) return
    ;(byLevel[level[n.id]] ??= []).push(n.id)
  })

  const pos: Record<string, { x: number; y: number }> = {}
  Object.entries(byLevel).forEach(([l, ids]) => {
    const y = Number(l) * 130
    ids.forEach((id, i) => {
      const offset = (i - (ids.length - 1) / 2) * 210
      pos[id] = { x: offset, y }
    })
  })
  graph.nodes.forEach((n) => {
    if (!compensators.has(n.id)) return
    const target = pos[n.compensates ?? ''] ?? { x: 0, y: 0 }
    pos[n.id] = { x: target.x + 120, y: target.y + 62 }
  })
  return pos
}

const EMPTY_GRAPH: Drawable = { nodes: [], edges: [] }

export function GraphCanvas({ graph, statuses, selected, onSelect }: {
  graph: Drawable | null | undefined
  statuses: Record<string, NodeStatus>
  selected: string | null
  onSelect: (id: string) => void
}) {
  // A caller mid-stream (the chat WorkflowPanel draws the *current* turn's
  // graph as it arrives) can have a turn with no graph yet - render an empty
  // canvas rather than crash the whole tree on graph.nodes of undefined.
  const safeGraph = graph ?? EMPTY_GRAPH
  const pos = useMemo(() => layout(safeGraph), [safeGraph])

  const nodes: Node<Data>[] = useMemo(
    () => safeGraph.nodes.map((n) => ({
      id: n.id,
      type: 'aco',
      position: pos[n.id] ?? { x: 0, y: 0 },
      data: { node: n, status: statuses[n.id] ?? 'pending', onSelect, selected: selected === n.id },
    })),
    [safeGraph, pos, statuses, selected, onSelect],
  )

  const edges: Edge[] = useMemo(
    () => safeGraph.edges.map((e, i) => ({
      id: `e${i}`,
      source: e.from_node,
      target: e.to_node,
      animated: statuses[e.to_node] === 'running',
      // A typed edge is drawn solid; a bare control edge is dashed. The
      // distinction is the whole contract story, so it should be visible.
      style: Object.keys(e.handoff_schema ?? {}).length
        ? undefined
        : { strokeDasharray: '4 4' },
    })),
    [safeGraph, statuses],
  )

  return (
    <ReactFlow
      nodes={nodes} edges={edges} nodeTypes={nodeTypes}
      fitView fitViewOptions={{ padding: 0.18 }}
      minZoom={0.2} proOptions={{ hideAttribution: true }}
      nodesDraggable={false} nodesConnectable={false}
    >
      <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#232329" />
      <Controls showInteractive={false} />
    </ReactFlow>
  )
}

export function StatusLegend() {
  return (
    <div className="flex items-center gap-3 flex-wrap">
      {(Object.keys(STATUS_COLOR) as NodeStatus[]).map((s) => (
        <span key={s} className="flex items-center gap-1.5 text-[10px] text-[var(--color-ink-faint)]">
          <span className="h-2 w-2 rounded-full" style={{ background: STATUS_COLOR[s] }} />
          {s}
        </span>
      ))}
    </div>
  )
}
