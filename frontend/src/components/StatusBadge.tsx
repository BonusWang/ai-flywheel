import React from 'react'

const STATUS_STYLES: Record<string, string> = {
  PASS: 'bg-ok-soft text-ok-ink border-ok/25',
  verified_active: 'bg-ok-soft text-ok-ink border-ok/25',
  processed: 'bg-ok-soft text-ok-ink border-ok/25',
  resolved: 'bg-ok-soft text-ok-ink border-ok/25',

  RUNNING: 'bg-accent-soft text-accent-ink border-accent/30 animate-pulse',
  in_progress: 'bg-accent-soft text-accent-ink border-accent/30',
  in_review: 'bg-accent-soft text-accent-ink border-accent/30',

  AWAITING_APPROVAL: 'bg-amber-soft text-amber-ink border-amber/35',
  unverified_active: 'bg-amber-soft text-amber-ink border-amber/30',
  PARTIAL: 'bg-amber-soft text-amber-ink border-amber/30',
  pending: 'bg-amber-soft text-amber-ink border-amber/30',
  open: 'bg-amber-soft text-amber-ink border-amber/30',

  FAILED: 'bg-hot-soft text-hot border-hot/30',
  needs_revision: 'bg-hot-soft text-hot border-hot/30',
  high: 'bg-hot-soft text-hot border-hot/30',

  PENDING: 'bg-bg-sunk text-ink-4 border-line',
  SKIPPED: 'bg-bg-sunk text-ink-4 border-line',
  retired: 'bg-bg-sunk text-ink-4 border-line',
  dismissed: 'bg-bg-sunk text-ink-4 border-line',
}

const LABELS: Record<string, string> = {
  unverified_active: '待自然验证',
  verified_active: '已验证生效',
  needs_revision: '复发待修订',
  retired: '已退役',
  AWAITING_APPROVAL: '待审核放行',
  PASS: 'PASS',
  FAILED: 'FAILED',
  PARTIAL: 'PARTIAL',
  RUNNING: '运行中',
  PENDING: '待执行',
  SKIPPED: '已跳过',
  pending: '待审查',
  in_progress: '审查中',
  processed: '已沉淀',
  open: '待复盘',
  resolved: '已闭环',
  dismissed: '已忽略',
}

export const StatusBadge: React.FC<{ status: string; className?: string }> = ({
  status,
  className = '',
}) => {
  const style = STATUS_STYLES[status] || 'bg-bg-sunk text-ink-3 border-line'
  const label = LABELS[status] || status
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 text-[11.5px] font-medium rounded-full border ${style} ${className}`}
    >
      {label}
    </span>
  )
}
