import React from 'react'
import { MarkdownView } from '../MarkdownView'
import { PillTabs } from '../PageLayout'
import { StatusBadge } from '../StatusBadge'
import {
  APPROVAL_MODE_LABELS,
  CLIENT_LABELS,
  formatRelativeTime,
  formatRunnerMode,
  labelOf,
} from '../../lib/labels'
import type { AuditEventRecord, SessionRecord, WorkflowRunRecord } from '../../types'
import { ApprovalDrawer } from './ApprovalDrawer'
import { NodeDetailPanel } from './NodeDetailPanel'
import { NoiseFunnelStrip } from './NoiseFunnelStrip'

export type WorkflowDetailTab = 'stages' | 'digest' | 'sessions' | 'events'

/**
 * The expanded part of one batch row: the approval drawer (when the batch waits for
 * it), the noise funnel, the four detail tabs and their content.
 */
export const RunDetailPanel: React.FC<{
  run: WorkflowRunRecord
  sessions: SessionRecord[]
  events: AuditEventRecord[]
  detailTab: WorkflowDetailTab
  selectedNodeId: string
  approvedIndices: number[]
  busy: boolean
  feishuLabel: string
  onTabChange: (tab: WorkflowDetailTab) => void
  onSelectNode: (nodeId: string) => void
  onToggleIndex: (idx: number) => void
  onApprove: () => void
  onReject: () => void
}> = ({
  run,
  sessions,
  events,
  detailTab,
  selectedNodeId,
  approvedIndices,
  busy,
  feishuLabel,
  onTabChange,
  onSelectNode,
  onToggleIndex,
  onApprove,
  onReject,
}) => {
  const isAwaiting = run.status === 'AWAITING_APPROVAL'
  const activeNode =
    run.nodes.find((n) => n.node_id === selectedNodeId) || run.nodes[0] || null
  const batchSessions = sessions.filter((s) => run.admitted_sessions.includes(s.session_key))

  return (
    <div className="border-t border-line-soft bg-bg-sunk/45 px-5 py-4 space-y-4">
      {/* Inline Approval Section (attached directly to this date's batch!) */}
      {isAwaiting && (
        <ApprovalDrawer
          run={run}
          approvedIndices={approvedIndices}
          busy={busy}
          feishuLabel={feishuLabel}
          onToggleIndex={onToggleIndex}
          onApprove={onApprove}
          onReject={onReject}
        />
      )}

      {/* Noise filter funnel & model telemetry strip */}
      <NoiseFunnelStrip run={run} />

      {/* Detail sub-tabs */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <PillTabs
          items={[
            { key: 'stages', label: '各环节进度' },
            { key: 'digest', label: '本批日报' },
            {
              key: 'sessions',
              label: '涉及对话',
              count: run.admitted_sessions.length,
            },
            {
              key: 'events',
              label: '操作记录',
              count: events.length || undefined,
            },
          ]}
          active={detailTab}
          onChange={(k) => onTabChange(k as WorkflowDetailTab)}
        />
        <span className="text-[12px] text-ink-4">
          {labelOf(APPROVAL_MODE_LABELS, run.approval_mode, '智能审批')} ·{' '}
          {formatRunnerMode(run.runner_mode)} · <span className="mono">{run.run_id}</span>
        </span>
      </div>

      {detailTab === 'stages' && (
        <NodeDetailPanel
          nodes={run.nodes}
          activeNode={activeNode}
          onSelectNode={onSelectNode}
        />
      )}

      {detailTab === 'digest' && (
        <div className="card p-4">
          {run.digest_markdown ? (
            <MarkdownView content={run.digest_markdown} />
          ) : (
            <p className="text-[13px] text-ink-4">
              本批尚未完成全部步骤，审批通过后会自动生成总结日报。
            </p>
          )}
        </div>
      )}

      {detailTab === 'sessions' && (
        <div className="card p-4">
          {batchSessions.length === 0 ? (
            <p className="text-[13px] text-ink-4">
              本批共纳入 {run.admitted_sessions.length} 段对话记录。
            </p>
          ) : (
            <div className="divide-y divide-line-soft">
              {batchSessions.map((s) => (
                <div
                  key={s.session_key}
                  className="py-2.5 flex items-center justify-between gap-3"
                >
                  <div className="min-w-0">
                    <div className="text-[13px] font-medium text-ink truncate">
                      {s.title || s.session_key}
                    </div>
                    <div className="text-[11.5px] text-ink-4">
                      {labelOf(CLIENT_LABELS, s.client)}
                      {s.summary ? ` · ${s.summary}` : ''}
                    </div>
                  </div>
                  <StatusBadge status={s.status} />
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {detailTab === 'events' && (
        <div className="card p-4">
          {events.length === 0 ? (
            <p className="text-[13px] text-ink-4">暂无审计记录。</p>
          ) : (
            <div className="divide-y divide-line-soft">
              {events.map((ev) => (
                <div
                  key={ev.seq}
                  className="py-2 flex flex-wrap items-center justify-between gap-2 text-[12.5px]"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="chip mono">{ev.event_type}</span>
                    <span className="text-ink-2">{ev.summary}</span>
                  </div>
                  <span className="num text-[11.5px] text-ink-4">
                    {formatRelativeTime(ev.created_at)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
