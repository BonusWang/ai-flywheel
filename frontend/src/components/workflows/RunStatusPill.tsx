import React from 'react'
import {
  Check,
  CheckCircle2,
  Loader2,
  Minus,
  Pause,
  X,
  XCircle,
} from 'lucide-react'
import { STAGE_PLAIN_NAMES } from '../../lib/labels'
import type { NodeExecution, WorkflowRunRecord } from '../../types'

/** The batch's overall status pill at the head of its pipeline row. */
export const RunStatusPill: React.FC<{ status: WorkflowRunRecord['status'] }> = ({ status }) => {
  switch (status) {
    case 'PASS':
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[var(--radius-mark)] border border-ok/35 bg-ok-soft text-ok-ink text-[12px] font-semibold min-w-[78px] justify-center transition-colors duration-200">
          <CheckCircle2 className="w-3.5 h-3.5" />
          已通过
        </span>
      )
    case 'AWAITING_APPROVAL':
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[var(--radius-mark)] border border-amber/45 bg-amber-soft text-amber-ink text-[12px] font-semibold min-w-[78px] justify-center transition-colors duration-200">
          <span className="status-dot-live bg-amber" />
          待审批
        </span>
      )
    case 'RUNNING':
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[var(--radius-mark)] border border-accent/40 bg-accent-soft text-accent-ink text-[12px] font-semibold min-w-[78px] justify-center transition-colors duration-200">
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
          运行中
        </span>
      )
    case 'FAILED':
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[var(--radius-mark)] border border-hot/40 bg-hot-soft text-hot text-[12px] font-semibold min-w-[78px] justify-center transition-colors duration-200">
          <XCircle className="w-3.5 h-3.5" />
          已失败
        </span>
      )
    case 'CANCELLED':
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[var(--radius-mark)] border border-line-strong bg-bg-sunk text-ink-3 text-[12px] font-semibold min-w-[78px] justify-center transition-colors duration-200">
          <XCircle className="w-3.5 h-3.5" />
          已取消
        </span>
      )
    case 'PARTIAL':
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[var(--radius-mark)] border border-amber/40 bg-amber-soft text-amber-ink text-[12px] font-semibold min-w-[78px] justify-center transition-colors duration-200">
          <Minus className="w-3.5 h-3.5" />
          部分完成
        </span>
      )
    default:
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[var(--radius-mark)] border border-line-strong bg-bg-sunk text-ink-3 text-[12px] font-semibold min-w-[78px] justify-center transition-colors duration-200">
          <Minus className="w-3.5 h-3.5" />
          未开始
        </span>
      )
  }
}

/** One connected dot in the 8-stage line; clicking it opens that node's detail. */
export const StageDot: React.FC<{
  node: NodeExecution
  active: boolean
  onClick: () => void
}> = ({ node, active, onClick }) => {
  const plainName = STAGE_PLAIN_NAMES[node.node_id] || node.name
  const shortCode = node.node_id.split('_')[0]
  const tooltip = `${shortCode} ${plainName}：${node.summary || '尚未执行'}`

  let ringCls = 'border-line-strong bg-surface text-ink-4'
  let icon: React.ReactNode = <span className="size-1.5 rounded-full bg-line-strong" />

  if (node.status === 'PASS') {
    ringCls = 'border-ok bg-surface text-ok'
    icon = <Check className="w-3 h-3 stroke-[2.6] animate-check-pop" />
  } else if (node.status === 'AWAITING_APPROVAL') {
    ringCls = 'border-amber bg-amber-soft text-amber-ink status-dot-ring'
    icon = <Pause className="w-3 h-3 stroke-[2.5]" />
  } else if (node.status === 'RUNNING') {
    ringCls = 'border-accent bg-accent-soft text-accent status-dot-ring'
    icon = <Loader2 className="w-3 h-3 animate-spin" />
  } else if (node.status === 'FAILED') {
    ringCls = 'border-hot bg-hot-soft text-hot'
    icon = <X className="w-3 h-3 stroke-[2.6]" />
  } else if (node.status === 'PARTIAL') {
    ringCls = 'border-amber bg-amber-soft text-amber-ink'
    icon = <Minus className="w-3 h-3 stroke-[2.6]" />
  } else if (node.status === 'SKIPPED') {
    ringCls = 'border-line bg-bg-sunk text-ink-4'
    icon = <Minus className="w-3 h-3" />
  }

  return (
    <button
      type="button"
      title={tooltip}
      aria-label={tooltip}
      onClick={onClick}
      className={`pressable-sm relative flex size-6 shrink-0 items-center justify-center rounded-full border-[1.5px] transition-[transform,border-color,background-color,box-shadow] duration-[180ms] ease-[var(--ease-out-quint)] hover:scale-110 ${ringCls} ${
        active ? 'ring-2 ring-accent/45 scale-110' : ''
      }`}
    >
      {icon}
    </button>
  )
}
