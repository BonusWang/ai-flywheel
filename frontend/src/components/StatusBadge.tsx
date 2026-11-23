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
  unverified_active: '试运行',
  verified_active: '已生效',
  needs_revision: '需修改',
  retired: '已停用',
  AWAITING_APPROVAL: '待审批',
  PASS: '已通过',
  FAILED: '已失败',
  PARTIAL: '已取消',
  RUNNING: '运行中',
  PENDING: '未开始',
  SKIPPED: '已跳过',
  pending: '待整理',
  in_progress: '整理中',
  processed: '已整理',
  open: '待查看',
  resolved: '已处理',
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
