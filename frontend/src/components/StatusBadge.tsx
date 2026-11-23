import React from 'react'

const STATUS_STYLES: Record<string, string> = {
  PASS: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  verified_active: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  processed: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  resolved: 'bg-emerald-50 text-emerald-700 border-emerald-200',

  RUNNING: 'bg-blue-50 text-blue-700 border-blue-200 animate-pulse',
  in_progress: 'bg-blue-50 text-blue-700 border-blue-200',
  in_review: 'bg-blue-50 text-blue-700 border-blue-200',

  AWAITING_APPROVAL: 'bg-amber-50 text-amber-800 border-amber-300',
  unverified_active: 'bg-amber-50 text-amber-800 border-amber-200',
  PARTIAL: 'bg-amber-50 text-amber-800 border-amber-200',
  pending: 'bg-amber-50 text-amber-700 border-amber-200',
  open: 'bg-amber-50 text-amber-800 border-amber-200',

  FAILED: 'bg-rose-50 text-rose-700 border-rose-200',
  needs_revision: 'bg-rose-50 text-rose-700 border-rose-200',
  high: 'bg-rose-50 text-rose-700 border-rose-200',

  PENDING: 'bg-slate-100 text-slate-600 border-slate-200',
  SKIPPED: 'bg-slate-100 text-slate-500 border-slate-200',
  retired: 'bg-slate-100 text-slate-500 border-slate-200',
  dismissed: 'bg-slate-100 text-slate-500 border-slate-200',
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
  const style = STATUS_STYLES[status] || 'bg-slate-100 text-slate-700 border-slate-200'
  const label = LABELS[status] || status
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full border ${style} ${className}`}
    >
      {label}
    </span>
  )
}
