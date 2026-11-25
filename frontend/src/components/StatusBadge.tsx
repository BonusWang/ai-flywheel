import React from 'react'
import {
  DISSENT_STATUS_LABELS,
  OUTCOME_STATUS_LABELS,
  RULE_STATUS_LABELS,
} from '../lib/labels'

const STATUS_STYLES: Record<string, string> = {
  PASS: 'bg-ok-soft text-ok-ink border-ok/25',
  success: 'bg-ok-soft text-ok-ink border-ok/25',
  verified_active: 'bg-ok-soft text-ok-ink border-ok/25',
  processed: 'bg-ok-soft text-ok-ink border-ok/25',
  resolved: 'bg-ok-soft text-ok-ink border-ok/25',
  merged: 'bg-ok-soft text-ok-ink border-ok/25',

  RUNNING: 'bg-accent-soft text-accent-ink border-accent/30',
  in_progress: 'bg-accent-soft text-accent-ink border-accent/30',
  in_review: 'bg-accent-soft text-accent-ink border-accent/30',

  AWAITING_APPROVAL: 'bg-amber-soft text-amber-ink border-amber/35',
  unverified_active: 'bg-amber-soft text-amber-ink border-amber/30',
  PARTIAL: 'bg-amber-soft text-amber-ink border-amber/30',
  partial: 'bg-amber-soft text-amber-ink border-amber/30',
  CANCELLED: 'bg-bg-sunk text-ink-3 border-line-strong',
  pending: 'bg-amber-soft text-amber-ink border-amber/30',
  open: 'bg-amber-soft text-amber-ink border-amber/30',

  FAILED: 'bg-hot-soft text-hot border-hot/30',
  failed: 'bg-hot-soft text-hot border-hot/30',
  needs_revision: 'bg-hot-soft text-hot border-hot/30',
  high: 'bg-hot-soft text-hot border-hot/30',
  red_silent: 'bg-hot-soft text-hot border-hot/35',

  yellow_caught: 'bg-amber-soft text-amber-ink border-amber/35',
  observing: 'bg-amber-soft text-amber-ink border-amber/30',
  needs_recheck: 'bg-amber-soft text-amber-ink border-amber/35',

  green_minor: 'bg-ok-soft text-ok-ink border-ok/25',
  promoted: 'bg-ok-soft text-ok-ink border-ok/25',

  PENDING: 'bg-bg-sunk text-ink-4 border-line',
  SKIPPED: 'bg-bg-sunk text-ink-4 border-line',
  dormant: 'bg-bg-sunk text-ink-3 border-line-strong',
  retired: 'bg-bg-sunk text-ink-4 border-line',
  archived: 'bg-bg-sunk text-ink-4 border-line',
  dismissed: 'bg-bg-sunk text-ink-4 border-line',
}

const LABELS: Record<string, string> = {
  ...RULE_STATUS_LABELS,
  ...OUTCOME_STATUS_LABELS,
  ...DISSENT_STATUS_LABELS,
  red_silent: '漏检失误',
  yellow_caught: '自查拦截',
  green_minor: '轻微',
  observing: '观察中',
  promoted: '已升规则',
  archived: '已归档',
  needs_recheck: '待复核',
  AWAITING_APPROVAL: '待审批',
  PASS: '已通过',
  FAILED: '已失败',
  PARTIAL: '部分完成',
  CANCELLED: '已取消',
  RUNNING: '运行中',
  PENDING: '未开始',
  SKIPPED: '已跳过',
  pending: '待整理',
  in_progress: '整理中',
  processed: '已沉淀',
  open: '待处理',
  resolved: '已处理',
  dismissed: '已忽略',
}

const SEVERITY_DOT: Record<string, string> = {
  red_silent: 'bg-hot',
  yellow_caught: 'bg-amber',
  green_minor: 'bg-ok',
}

export const StatusBadge: React.FC<{ status: string; className?: string }> = ({
  status,
  className = '',
}) => {
  const style = STATUS_STYLES[status] || 'bg-bg-sunk text-ink-3 border-line'
  const label = LABELS[status] || status
  const dotColor = SEVERITY_DOT[status]
  const isLive = status === 'RUNNING' || status === 'in_progress'

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 text-[11.5px] font-medium rounded-full border transition-colors duration-[150ms] ${style} ${className}`}
    >
      {isLive && <span className="status-dot-live bg-accent shrink-0" />}
      {!isLive && dotColor && (
        <span className={`size-1.5 rounded-full shrink-0 ${dotColor}`} />
      )}
      <span>{label}</span>
    </span>
  )
}
