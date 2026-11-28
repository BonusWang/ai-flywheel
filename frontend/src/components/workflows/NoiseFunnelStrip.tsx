import React from 'react'
import type { WorkflowRunRecord } from '../../types'

/**
 * The triage & extraction funnel strip of one batch.
 * Only renders when triage or review findings exist.
 */
export const NoiseFunnelStrip: React.FC<{ run: WorkflowRunRecord }> = ({ run }) => {
  const p00 = run.nodes.find((n) => n.node_id === 'P00_PREPARE')
  const p02 = run.nodes.find((n) => n.node_id === 'P02_REVIEW')
  const triage = p00?.payload?.triage_report ?? undefined
  const findings = p02?.payload?.findings || []
  const worthyCount = findings.filter((f) => f?.worthy !== false).length
  const hasFunnel = (triage && (triage.checked || 0) > 0) || findings.length > 0
  if (!hasFunnel) return null

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-[var(--radius-control)] border border-line-soft bg-surface px-3.5 py-2 text-[12px] text-ink-3 num">
      <span className="font-semibold text-ink">筛选明细</span>
      {triage && (triage.checked || 0) > 0 && (
        <span>
          初筛 {triage.checked} 段对话，跳过{' '}
          <strong className="text-ink">{triage.skipped || 0}</strong> 段闲聊
        </span>
      )}
      {findings.length > 0 && (
        <>
          <span>·</span>
          <span>
            提炼出有效经验 <strong className="text-ok-ink">{worthyCount}</strong> / {findings.length}
          </span>
        </>
      )}
      <span>·</span>
      <span>
        拟写入 <strong className="text-ink">{run.planned_writes.length}</strong> 项
      </span>
    </div>
  )
}
