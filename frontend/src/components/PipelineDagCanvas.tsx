import React from 'react'
import {
  CheckCircle2,
  Clock,
  AlertTriangle,
  XCircle,
  PauseCircle,
  Loader2,
  MinusCircle,
  ShieldCheck,
} from 'lucide-react'
import type { NodeExecution } from '../types'

interface Props {
  nodes: NodeExecution[]
  selectedNodeId: string | null
  onSelectNode: (nodeId: string) => void
}

function NodeIcon({ status }: { status: NodeExecution['status'] }) {
  switch (status) {
    case 'PASS':
      return <CheckCircle2 className="w-4 h-4 text-emerald-600" />
    case 'RUNNING':
      return <Loader2 className="w-4 h-4 text-blue-600 animate-spin" />
    case 'AWAITING_APPROVAL':
      return <PauseCircle className="w-4 h-4 text-amber-600 animate-pulse" />
    case 'FAILED':
      return <XCircle className="w-4 h-4 text-rose-600" />
    case 'PARTIAL':
      return <AlertTriangle className="w-4 h-4 text-amber-600" />
    case 'SKIPPED':
      return <MinusCircle className="w-4 h-4 text-slate-400" />
    default:
      return <Clock className="w-4 h-4 text-slate-400" />
  }
}

const BORDER_BY_STATUS: Record<string, string> = {
  PASS: 'border-emerald-300 bg-emerald-50/40',
  RUNNING: 'border-blue-400 bg-blue-50/50 ring-2 ring-blue-100',
  AWAITING_APPROVAL: 'border-amber-400 bg-amber-50/60 ring-2 ring-amber-100',
  FAILED: 'border-rose-400 bg-rose-50/60',
  PARTIAL: 'border-amber-300 bg-amber-50/40',
  SKIPPED: 'border-slate-200 bg-slate-50/70 opacity-75',
  PENDING: 'border-slate-200 bg-white',
}

export const PipelineDagCanvas: React.FC<Props> = ({
  nodes,
  selectedNodeId,
  onSelectNode,
}) => {
  const firstHalf = nodes.slice(0, 4)
  const secondHalf = nodes.slice(4, 8)
  const p04 = nodes.find((n) => n.node_id === 'P04_WRITE_VERIFY')
  const gatePaused = p04?.status === 'AWAITING_APPROVAL'

  const renderNode = (node: NodeExecution) => {
    const isSel = selectedNodeId === node.node_id
    const statusCls = BORDER_BY_STATUS[node.status] || BORDER_BY_STATUS.PENDING
    return (
      <button
        key={node.node_id}
        onClick={() => onSelectNode(node.node_id)}
        className={`relative text-left p-3.5 rounded-xl border transition-all cursor-pointer ${statusCls} ${
          isSel ? 'ring-2 ring-slate-900 border-slate-900 shadow-sm' : 'hover:shadow-xs'
        }`}
      >
        <div className="flex items-center justify-between gap-2 mb-1.5">
          <span className="font-mono text-[11px] font-bold tracking-tight text-slate-700">
            {node.node_id.split('_')[0]}
          </span>
          <NodeIcon status={node.status} />
        </div>
        <div className="text-sm font-semibold text-slate-900 leading-snug">{node.name}</div>
        <div className="text-[11px] text-slate-500 mt-1 line-clamp-2 min-h-[2rem]">
          {node.summary || '等待前置节点放行'}
        </div>
        <div className="mt-2 pt-2 border-t border-slate-200/70 flex items-center justify-between text-[11px] text-slate-500 font-mono">
          <span className="truncate max-w-[115px]">{node.actor}</span>
          <span>{node.duration_ms ? `${node.duration_ms}ms` : '--'}</span>
        </div>
      </button>
    )
  }

  return (
    <div className="bg-white border border-slate-200/90 rounded-2xl p-5 shadow-xs">
      <div className="flex flex-wrap items-center justify-between gap-2 mb-4">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">P00–P07 固定 8 节点工作流画布</h3>
          <p className="text-xs text-slate-500">点击任意节点下钻查看内部检查项、写入回执与 SHA-256 哈希</p>
        </div>
        <div className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border border-slate-200 bg-slate-50 text-slate-600">
          <ShieldCheck className="w-3.5 h-3.5 text-blue-600" />
          P03 → P04 受控审核门禁：{gatePaused ? '当前挂起等待批准' : '已配置'}
        </div>
      </div>

      {/* Stage 1: P00 -> P03 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {firstHalf.map(renderNode)}
      </div>

      {/* Visual Connector + Approval Gate */}
      <div className="my-3 flex items-center justify-center">
        <div className="flex items-center gap-2 px-3.5 py-1.5 rounded-full text-xs font-medium border border-dashed border-slate-300 bg-slate-50 text-slate-600">
          <span>P03 路由与写入计划完成</span>
          <span>→</span>
          <span
            className={`px-2 py-0.5 rounded-full font-semibold ${
              gatePaused
                ? 'bg-amber-100 text-amber-800'
                : 'bg-emerald-100 text-emerald-800'
            }`}
          >
            {gatePaused ? '⏸ 人工审核放行门禁' : '✓ 写入放行门禁'}
          </span>
          <span>→</span>
          <span>进入 P04 受控落盘与回读校验</span>
        </div>
      </div>

      {/* Stage 2: P04 -> P07 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {secondHalf.map(renderNode)}
      </div>
    </div>
  )
}
