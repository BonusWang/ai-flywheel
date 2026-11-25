import React from 'react'
import type { WorkflowRunRecord } from '../../types'

/**
 * The P00 → P02 → P03 noise-funnel strip of one batch.
 *
 * It only appears once there is triage or review data; a run that has not reached
 * those stages yet shows nothing rather than a row of zeros.
 */
export const NoiseFunnelStrip: React.FC<{ run: WorkflowRunRecord }> = ({ run }) => {
  const p00 = run.nodes.find((n) => n.node_id === 'P00_PREPARE')
  const p02 = run.nodes.find((n) => n.node_id === 'P02_REVIEW')
  const triage = (p00?.payload as any)?.triage_report as
    | { checked?: number; skipped?: number; admitted?: number }
    | undefined
  const findings = ((p02?.payload as any)?.findings || []) as Array<{
    worthy?: boolean
  }>
  const worthyCount = findings.filter((f) => f?.worthy !== false).length
  const hasFunnel = (triage && (triage.checked || 0) > 0) || findings.length > 0
  if (!hasFunnel) return null

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-[var(--radius-control)] border border-line-soft bg-surface px-3.5 py-2 text-[12px] text-ink-3 num">
      <span className="font-semibold text-ink">噪音过滤漏斗</span>
      {triage && (triage.checked || 0) > 0 && (
        <span>
          一级硬筛 {triage.checked} → 跳过噪音{' '}
          <strong className="text-ink">{triage.skipped || 0}</strong>
        </span>
      )}
      {findings.length > 0 && (
        <>
          <span>·</span>
          <span>
            P02 语义闸门有效 <strong className="text-ok-ink">{worthyCount}</strong> /{' '}
            {findings.length}
          </span>
        </>
      )}
      <span>·</span>
      <span>
        计划写入 <strong className="text-ink">{run.planned_writes.length}</strong> 项
      </span>
    </div>
  )
}
