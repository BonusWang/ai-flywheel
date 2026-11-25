import React from 'react'
import { Calendar, ChevronDown, Clock } from 'lucide-react'
import {
  formatDateYMD,
  formatDuration,
  formatRelativeTime,
  labelOf,
  PHASE_LABELS,
  TRIGGER_LABELS,
} from '../../lib/labels'
import type { AuditEventRecord, SessionRecord, WorkflowRunRecord } from '../../types'
import { Collapse } from '../ui/Collapse'
import { RunDetailPanel, type WorkflowDetailTab } from './RunDetailPanel'
import { RunStatusPill, StageDot } from './RunStatusPill'

/**
 * One batch pipeline row with smooth interruptible detail disclosure (`Collapse`),
 * dual-layer progress connector lines, and single rotating chevron.
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
  onSelectAll: (selectAll: boolean) => void
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
  onSelectAll,
  onApprove,
  onReject,
}) => {
  const isAwaiting = run.status === 'AWAITING_APPROVAL'
  const batchDate = run.batch_date || formatDateYMD(run.started_at)

  return (
    <div className={`transition-colors duration-200 ${isAwaiting ? 'bg-amber-soft/20' : 'bg-surface'}`}>
      <div className="px-4 py-3.5 flex flex-wrap lg:flex-nowrap items-center gap-4 hover:bg-bg-sunk/40 transition-colors duration-[150ms]">
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
              <span className="px-1.5 py-0.2 rounded-[var(--radius-mark)] bg-ok text-ok-contrast text-[10.5px] font-semibold">
                最新
              </span>
            )}
          </div>
          <div className="mt-1 flex items-center gap-2 text-[12px] text-ink-3 flex-wrap">
            <span>{labelOf(PHASE_LABELS, run.phase, '每日整理')}</span>
            <span className="text-ink-4">·</span>
            <span>{labelOf(TRIGGER_LABELS, run.trigger)}</span>
            <span className="text-ink-4">·</span>
            <span className="num">{run.admitted_sessions.length} 段对话</span>
          </div>
        </div>

        {/* 3. Horizontal 8-Stage Connected Nodes with fluid progress lines */}
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
                  <span className="relative h-[2px] w-4 sm:w-5 shrink-0 overflow-hidden rounded-full bg-line-strong">
                    <span
                      className={`absolute inset-0 origin-left bg-ok transition-transform duration-[300ms] ease-[var(--ease-out-quint)] ${
                        prevDone ? 'scale-x-100' : 'scale-x-0'
                      }`}
                    />
                  </span>
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

        {/* 5. Action Button (single rotating chevron) */}
        <div className="shrink-0 ml-auto lg:ml-0">
          {isAwaiting ? (
            <button
              type="button"
              aria-expanded={isExpanded}
              onClick={() => onToggleRow(run)}
              className="pressable inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] bg-amber text-amber-contrast text-[12px] font-semibold hover:opacity-90"
            >
              <span>审批本批 ({run.planned_writes.length})</span>
              <ChevronDown
                className={`w-3.5 h-3.5 transition-transform duration-200 ease-[var(--ease-out-quint)] ${
                  isExpanded ? 'rotate-180' : ''
                }`}
              />
            </button>
          ) : (
            <button
              type="button"
              aria-expanded={isExpanded}
              onClick={() => onToggleRow(run)}
              className="pressable inline-flex items-center gap-1 h-8 px-2.5 rounded-[var(--radius-control)] border border-line bg-surface text-[12px] text-ink-3 hover:border-line-strong hover:text-ink"
            >
              <span>{isExpanded ? '收起' : '查看'}</span>
              <ChevronDown
                className={`w-3.5 h-3.5 transition-transform duration-200 ease-[var(--ease-out-quint)] ${
                  isExpanded ? 'rotate-180' : ''
                }`}
              />
            </button>
          )}
        </div>
      </div>

      {/* Smooth interruptible batch detail & approval drawer */}
      <Collapse open={isExpanded} lazy>
        <RunDetailPanel
          run={run}
          isExpanded={isExpanded}
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
          onSelectAll={onSelectAll}
          onApprove={onApprove}
          onReject={onReject}
        />
      </Collapse>
    </div>
  )
}
