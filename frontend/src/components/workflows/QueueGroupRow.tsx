import React, { useMemo } from 'react'
import {
  Ban,
  Check,
  ChevronDown,
  UserX,
  Zap,
} from 'lucide-react'
import { formatRelativeTime, labelOf } from '../../lib/labels'
import type { AuditEventRecord, DistillQueueItem, QueueAction } from '../../types'
import { Collapse } from '../ui/Collapse'
import { ACTION_LABELS, Badge, QueueRow, priorityTone } from './QueueRow'

export interface QueueGroup {
  rootKey: string
  primaryItem: DistillQueueItem
  items: DistillQueueItem[]
  chunks: DistillQueueItem[]
  subagents: DistillQueueItem[]
}


export const QueueGroupRow: React.FC<{
  group: QueueGroup
  expanded: boolean
  statusToneClass: (status: string) => string
  statusLabels: Record<string, string>
  clientLabels: Record<string, string>
  channelLabels: Record<string, string>
  priorityLabels: Record<string, string>
  excludeReasonLabels: Record<string, string>
  selected: Set<string>
  expandedTrailId: string | null
  eventsByItem: Record<string, AuditEventRecord[]>
  loadingTrailId: string | null
  actingId: string | null
  triggerBusy: boolean
  bulkBusy: boolean
  onToggleGroupExpand: () => void
  onToggleChecked: (queueId: string) => void
  onToggleTrail: (item: DistillQueueItem) => void
  onAction: (item: DistillQueueItem, action: QueueAction) => void
  onTriggerQueueIds: (queueIds: string[]) => void
  onBulkAction: (action: QueueAction, ids: string[]) => void
  onJumpToRun: (runId: string) => void
}> = ({
  group,
  expanded,
  statusToneClass,
  statusLabels,
  clientLabels,
  channelLabels,
  priorityLabels,
  excludeReasonLabels,
  selected,
  expandedTrailId,
  eventsByItem,
  loadingTrailId,
  actingId,
  triggerBusy,
  bulkBusy,
  onToggleGroupExpand,
  onToggleChecked,
  onToggleTrail,
  onAction,
  onTriggerQueueIds,
  onBulkAction,
  onJumpToRun,
}) => {
  const { primaryItem, items, chunks, subagents } = group
  const selectableItems = items.filter(
    (i) => i.status !== 'processed' && i.status !== 'processing'
  )
  const allChecked =
    selectableItems.length > 0 && selectableItems.every((i) => selected.has(i.queue_id))
  const someChecked =
    selectableItems.some((i) => selected.has(i.queue_id)) && !allChecked

  const toggleAllInGroup = () => {
    for (const item of selectableItems) {
      if (allChecked) {
        if (selected.has(item.queue_id)) onToggleChecked(item.queue_id)
      } else {
        if (!selected.has(item.queue_id)) onToggleChecked(item.queue_id)
      }
    }
  }

  const queuedIds = items.filter((i) => i.status === 'queued').map((i) => i.queue_id)
  const canTriggerGroup = queuedIds.length > 0
  const canExcludeGroup = queuedIds.length > 0

  const wsShort = useMemo(() => {
    const ws = primaryItem.workspace || items.find((i) => i.workspace)?.workspace
    if (!ws) return ''
    const norm = ws.replace(/\\/g, '/').replace(/\/+$/, '')
    const parts = norm.split('/')
    return parts[parts.length - 1] || ws
  }, [primaryItem.workspace, items])

  const fullWs = primaryItem.workspace || items.find((i) => i.workspace)?.workspace || ''
  const groupTitle = primaryItem.title || primaryItem.session_key

  return (
    <div
      className={`group transition-colors duration-[150ms] ${
        allChecked ? 'bg-accent-softer' : 'hover:bg-bg-sunk/30'
      }`}
    >
      <div className="px-4 py-3">
        <div className="flex flex-wrap items-start gap-3">
          {selectableItems.length > 0 ? (
            <input
              type="checkbox"
              aria-label={`选择整组 ${groupTitle}`}
              checked={allChecked}
              ref={(el) => {
                if (el) el.indeterminate = someChecked
              }}
              onChange={toggleAllInGroup}
              className="mt-1 size-3.5 shrink-0 accent-accent cursor-pointer"
            />
          ) : (
            <span aria-hidden="true" className="mt-1 size-3.5 shrink-0" />
          )}

          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span
                className={`inline-flex h-[20px] items-center gap-1 rounded-full border px-2 text-[11px] font-semibold ${statusToneClass(
                  primaryItem.status
                )}`}
              >
                {labelOf(statusLabels, primaryItem.status, primaryItem.status)}
              </span>
              <span
                className="min-w-0 truncate text-[13px] font-medium text-ink cursor-pointer hover:text-accent"
                title={`${groupTitle} (${primaryItem.session_key})`}
                onClick={onToggleGroupExpand}
              >
                {groupTitle}
              </span>
            </div>

            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              {wsShort && (
                <Badge title={`工作区完整路径：${fullWs}`}>{wsShort}</Badge>
              )}
              <Badge title="来源工具">
                {labelOf(clientLabels, primaryItem.client, primaryItem.client)}
              </Badge>
              {chunks.length > 1 && (
                <Badge mono className="border-accent/35 bg-accent-soft text-accent-ink">
                  {chunks.length} 个分片
                </Badge>
              )}
              {subagents.length > 0 && (
                <Badge mono className="border-amber/40 bg-amber-soft text-amber-ink">
                  {subagents.length} 个子代理
                </Badge>
              )}
            </div>
          </div>

          <div className="ml-auto flex max-w-full shrink-0 flex-wrap items-center justify-end gap-1">
            {canTriggerGroup && (
              <button
                type="button"
                onClick={() => onTriggerQueueIds(queuedIds)}
                disabled={triggerBusy}
                className="pressable inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink text-[11.5px] font-medium hover:bg-accent/18 disabled:opacity-40"
                title="将整组对话（含分片与子代理）送入整理"
              >
                <Zap className="w-3.5 h-3.5" />
                整组插队 ({queuedIds.length})
              </button>
            )}
            {canExcludeGroup && (
              <button
                type="button"
                onClick={() => onBulkAction('exclude', queuedIds)}
                disabled={bulkBusy}
                className="pressable inline-flex items-center gap-1 h-7 px-2 rounded-[var(--radius-mark)] text-[11.5px] text-ink-3 hover:text-ink hover:bg-bg-sunk disabled:opacity-40"
                title="移出整组排队项"
              >
                <Ban className="w-3.5 h-3.5" />
                整组移出
              </button>
            )}
            <button
              type="button"
              onClick={onToggleGroupExpand}
              aria-expanded={expanded}
              className="pressable inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] border border-line bg-surface text-[11.5px] text-ink-2 hover:border-line-strong hover:text-ink"
            >
              <ChevronDown
                className={`w-3.5 h-3.5 transition-transform duration-200 ease-[var(--ease-out-quint)] ${
                  expanded ? 'rotate-180 text-accent' : ''
                }`}
              />
              <span>{expanded ? '收起明细' : `展开明细 (${items.length})`}</span>
            </button>
          </div>
        </div>
      </div>

      <Collapse open={expanded}>
        <div className="ml-6 border-l-2 border-line pl-3 pb-2 space-y-1">
          {items.map((childItem) => (
            <QueueRow
              key={childItem.queue_id}
              item={childItem}
              statusLabel={labelOf(statusLabels, childItem.status, childItem.status)}
              statusTone={statusToneClass(childItem.status)}
              clientLabel={labelOf(clientLabels, childItem.client, childItem.client)}
              channelLabel={labelOf(
                channelLabels,
                childItem.ingress_channel,
                childItem.ingress_channel
              )}
              priorityLabel={labelOf(priorityLabels, childItem.priority, '')}
              excludeReasonLabel={labelOf(
                excludeReasonLabels,
                childItem.exclude_reason_code,
                childItem.exclude_reason_code || '规则筛选'
              )}
              statusLabels={statusLabels}
              excludeReasonLabels={excludeReasonLabels}
              checked={selected.has(childItem.queue_id)}
              expanded={expandedTrailId === childItem.queue_id}
              events={eventsByItem[childItem.queue_id] ?? []}
              trailLoading={loadingTrailId === childItem.queue_id}
              acting={actingId === childItem.queue_id}
              triggerBusy={triggerBusy}
              isChild={true}
              onToggleChecked={() => onToggleChecked(childItem.queue_id)}
              onToggleTrail={() => onToggleTrail(childItem)}
              onAction={(action) => onAction(childItem, action)}
              onTrigger={() => onTriggerQueueIds([childItem.queue_id])}
              onJumpToRun={onJumpToRun}
            />
          ))}
        </div>
      </Collapse>
    </div>
  )
}
