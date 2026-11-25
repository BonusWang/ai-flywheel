import React from 'react'
import { Calendar, ChevronDown, ChevronUp, Clock } from 'lucide-react'
import {
  formatDateYMD,
  formatDuration,
  formatRelativeTime,
  labelOf,
  PHASE_LABELS,
  TRIGGER_LABELS,
} from '../../lib/labels'
import type { AuditEventRecord, SessionRecord, WorkflowRunRecord } from '../../types'
import { RunDetailPanel, type WorkflowDetailTab } from './RunDetailPanel'
import { RunStatusPill, StageDot } from './RunStatusPill'

/**
 * One GitLab-CI-style batch row: status pill, date and batch meta, the eight connected
 * stage dots, duration and the inline approval / expand action. Expanding the row
 * renders the detail panel underneath.
 */
export const RunPipelineRow: React.FC<{
  run: WorkflowRunRecord
  isLatest: boolean
  isExpanded: boolean
  batchNo: number
  selectedNodeId: string
  detailTab: WorkflowDetailTab
  sessions: SessionRecord[]
  events: AuditEventRecord[]
  approvedIndices: number[]
  busy: boolean
  feishuLabel: string
  onToggleRow: (run: WorkflowRunRecord, nodeId?: string) => void
  onTabChange: (tab: WorkflowDetailTab) => void
  onSelectNode: (nodeId: string) => void
  onToggleIndex: (idx: number) => void
  onApprove: () => void
  onReject: () => void
}> = ({
  run,
  isLatest,
  isExpanded,
  batchNo,
  selectedNodeId,
  detailTab,
  sessions,
  events,
  approvedIndices,
  busy,
  feishuLabel,
  onToggleRow,
  onTabChange,
  onSelectNode,
  onToggleIndex,
  onApprove,
  onReject,
}) => {
  const isAwaiting = run.status === 'AWAITING_APPROVAL'
  const batchDate = run.batch_date || formatDateYMD(run.started_at)

  return (
    <div className={isAwaiting ? 'bg-amber-soft/20' : 'bg-surface'}>
      {/* Pipeline Row (GitLab style) */}
      <div className="px-4 py-3.5 flex flex-wrap lg:flex-nowrap items-center gap-4 hover:bg-bg-sunk/40 transition-colors">
        {/* 1. Status Pill */}
        <div className="shrink-0">
          <RunStatusPill status={run.status} />
        </div>

        {/* 2. Date & Batch Meta */}
        <div className="min-w-[190px] lg:w-[250px] shrink-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="num text-[14px] font-semibold text-ink">{batchDate}</span>
            <span className="mono text-[12px] text-ink-4">
              #{String(batchNo).padStart(2, '0')}
            </span>
            {isLatest && (
              <span className="px-1.5 py-0.2 rounded-[var(--radius-mark)] bg-ok text-white text-[10.5px] font-semibold">
                最新
              </span>
            )}
          </div>
          <div className="mt-1 flex items-center gap-2 text-[12px] text-ink-3 flex-wrap">
            <span>{labelOf(PHASE_LABELS, run.phase, '每日沉淀')}</span>
            <span className="text-ink-4">·</span>
            <span>{labelOf(TRIGGER_LABELS, run.trigger)}</span>
            <span className="text-ink-4">·</span>
            <span>{run.admitted_sessions.length} 段对话</span>
          </div>
        </div>

        {/* 3. Horizontal 8-Stage Connected Nodes */}
        <div className="flex items-center flex-1 min-w-[260px] overflow-x-auto py-1">
          {run.nodes.map((node, nIdx) => {
            const prevDone = node.status === 'PASS'
            return (
              <React.Fragment key={node.node_id}>
                <StageDot
                  node={node}
                  active={
                    isExpanded && detailTab === 'stages' && selectedNodeId === node.node_id
                  }
                  onClick={() => onToggleRow(run, node.node_id)}
                />
                {nIdx < run.nodes.length - 1 && (
                  <span
                    className={`h-[1.5px] w-4 sm:w-5 shrink-0 ${
                      prevDone ? 'bg-ok/60' : 'bg-line-strong'
                    }`}
                  />
                )}
              </React.Fragment>
            )
          })}
        </div>

        {/* 4. Duration & Relative Time */}
        <div className="hidden sm:flex flex-col items-end text-[11.5px] text-ink-4 shrink-0 min-w-[92px]">
          <span className="mono inline-flex items-center gap-1 text-ink-3">
            <Clock className="w-3 h-3" />
            {formatDuration(run.started_at, run.finished_at, run.nodes)}
          </span>
          <span className="inline-flex items-center gap-1 mt-0.5">
            <Calendar className="w-3 h-3" />
            {formatRelativeTime(run.started_at)}
          </span>
        </div>

        {/* 5. Action Button (Inline Approval or Expand) */}
        <div className="shrink-0 ml-auto lg:ml-0">
          {isAwaiting ? (
            <button
              type="button"
              onClick={() => onToggleRow(run)}
              className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] bg-amber text-white text-[12px] font-semibold hover:opacity-90"
            >
              审批本批 ({run.planned_writes.length})
              {isExpanded ? (
                <ChevronUp className="w-3.5 h-3.5" />
              ) : (
                <ChevronDown className="w-3.5 h-3.5" />
              )}
            </button>
          ) : (
            <button
              type="button"
              onClick={() => onToggleRow(run)}
              className="inline-flex items-center gap-1 h-8 px-2.5 rounded-[var(--radius-control)] border border-line bg-surface text-[12px] text-ink-3 hover:border-line-strong hover:text-ink"
            >
              {isExpanded ? '收起' : '查看'}
              {isExpanded ? (
                <ChevronUp className="w-3.5 h-3.5" />
              ) : (
                <ChevronDown className="w-3.5 h-3.5" />
              )}
            </button>
          )}
        </div>
      </div>

      {/* Expanded Inline Batch Detail & Approval Drawer */}
      {isExpanded && (
        <RunDetailPanel
          run={run}
          sessions={sessions}
          events={events}
          detailTab={detailTab}
          selectedNodeId={selectedNodeId}
          approvedIndices={approvedIndices}
          busy={busy}
          feishuLabel={feishuLabel}
          onTabChange={onTabChange}
          onSelectNode={onSelectNode}
          onToggleIndex={onToggleIndex}
          onApprove={onApprove}
          onReject={onReject}
        />
      )}
    </div>
  )
}
