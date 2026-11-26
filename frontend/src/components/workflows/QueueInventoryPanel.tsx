import React, { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  Ban,
  Check,
  ChevronDown,
  Loader2,
  RotateCcw,
  UserCheck,
  UserX,
  Wand2,
  X,
  Zap,
} from 'lucide-react'
import { api } from '../../api'
import {
  CLIENT_LABELS,
  INGRESS_CHANNEL_LABELS,
  QUEUE_STATUS_LABELS,
  dictLabels,
  formatRelativeTime,
  labelOf,
} from '../../lib/labels'
import type {
  AuditEventRecord,
  DictItem,
  DistillQueueItem,
  QueueAction,
  QueueReTriageChange,
  QueueReTriageResult,
} from '../../types'
import { ListToolbar, PaginationBar, usePagination } from '../ListControls'
import { Collapse } from '../ui/Collapse'
import { useToast } from '../ui/Toast'

const STATUS_COLOR_FALLBACK: Record<string, string> = {
  queued: 'blue',
  processing: 'amber',
  processed: 'green',
  excluded: 'gray',
  withdrawn: 'gray',
  dead_letter: 'red',
}

const STATUS_TONE: Record<string, string> = {
  blue: 'border-accent/35 bg-accent-soft text-accent-ink',
  amber: 'border-amber/40 bg-amber-soft text-amber-ink',
  green: 'border-ok/35 bg-ok-soft text-ok-ink',
  gray: 'border-line-strong bg-bg-sunk text-ink-3',
  red: 'border-hot/40 bg-hot-soft text-hot',
}

const NEUTRAL_BADGE = 'border-line bg-bg-sunk text-ink-3'

const ACTION_LABELS: Record<QueueAction, string> = {
  requeue: '重新加入',
  exclude: '移出队列',
  withdraw: '撤回',
  opt_out: '不再整理',
  clear_opt_out: '恢复整理',
}

const Badge: React.FC<{
  children: React.ReactNode
  className?: string
  mono?: boolean
  title?: string
}> = ({ children, className = '', mono = false, title }) => (
  <span
    title={title}
    className={`inline-flex h-[19px] max-w-[260px] items-center truncate rounded-[var(--radius-mark)] border px-1.5 text-[11px] font-medium ${
      mono ? 'mono' : ''
    } ${className || NEUTRAL_BADGE}`}
  >
    {children}
  </span>
)

function priorityTone(priority: string): string {
  if (priority === 'P0_CORE') return 'border-hot bg-hot text-white'
  if (priority === 'P1_SIGNAL') return 'border-amber/40 bg-amber-soft text-amber-ink'
  return NEUTRAL_BADGE
}

/** One compact, scannable queue row with contextual actions and smooth collapsible trail. */
const QueueRow: React.FC<{
  item: DistillQueueItem
  statusLabel: string
  statusTone: string
  clientLabel: string
  channelLabel: string
  priorityLabel: string
  excludeReasonLabel: string
  statusLabels: Record<string, string>
  excludeReasonLabels: Record<string, string>
  checked: boolean
  expanded: boolean
  events: AuditEventRecord[]
  trailLoading: boolean
  acting: boolean
  triggerBusy: boolean
  isChild?: boolean
  onToggleChecked: () => void
  onToggleTrail: () => void
  onAction: (action: QueueAction) => void
  onTrigger: () => void
  onJumpToRun: (runId: string) => void
}> = ({
  item,
  statusLabel,
  statusTone,
  clientLabel,
  channelLabel,
  priorityLabel,
  excludeReasonLabel,
  statusLabels,
  excludeReasonLabels,
  checked,
  expanded,
  events,
  trailLoading,
  acting,
  triggerBusy,
  isChild = false,
  onToggleChecked,
  onToggleTrail,
  onAction,
  onTrigger,
  onJumpToRun,
}) => {
  const isOptOut = item.exclude_reason_code === 'USER_OPT_OUT'
  const isProcessed = item.status === 'processed'
  const isProcessing = item.status === 'processing'
  const canSelect = !isProcessed && !isProcessing
  const canTrigger = item.status === 'queued'
  const canRequeue =
    item.status === 'excluded' ||
    item.status === 'withdrawn' ||
    item.status === 'dead_letter'
  const title = item.title || item.session_key

  const wsShort = useMemo(() => {
    if (!item.workspace) return ''
    const norm = item.workspace.replace(/\\/g, '/').replace(/\/+$/, '')
    const parts = norm.split('/')
    return parts[parts.length - 1] || item.workspace
  }, [item.workspace])

  const subagentInfo = useMemo(() => {
    if (!item.is_subagent) return null
    const payload = (item.payload || {}) as Record<string, any>
    const nickname = (payload.subagent_nickname as string) || ''
    const role = (payload.subagent_role as string) || ''
    return { nickname, role }
  }, [item.is_subagent, item.payload])

  const quickBtn =
    'pressable inline-flex items-center gap-1 h-7 px-2 rounded-[var(--radius-mark)] text-[11.5px] text-ink-3 hover:text-ink hover:bg-bg-sunk disabled:opacity-40'

  let reasonLine = ''
  if (item.status === 'excluded') {
    reasonLine = `移出原因：${excludeReasonLabel}${
      item.exclude_reason_detail ? ` · ${item.exclude_reason_detail}` : ''
    }`
  } else if (item.status === 'dead_letter') {
    reasonLine = `失败原因：${item.last_error || '重试已达上限，可重新加入再次尝试'}`
  }

  return (
    <div
      className={`group transition-colors duration-[150ms] ${
        isChild
          ? 'my-1 rounded-[var(--radius-mark)] border border-line/60 bg-surface/80 px-3 py-2'
          : 'px-4 py-3'
      } ${checked ? 'bg-accent-softer' : 'hover:bg-bg-sunk/40'}`}
    >
      <div className="flex flex-wrap items-start gap-3">
        {canSelect ? (
          <input
            type="checkbox"
            aria-label={`选择 ${title}`}
            checked={checked}
            onChange={onToggleChecked}
            className="mt-1 size-3.5 shrink-0 accent-accent cursor-pointer"
          />
        ) : (
          <span aria-hidden="true" className="mt-1 size-3.5 shrink-0" />
        )}

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span
              className={`inline-flex h-[20px] items-center gap-1 rounded-full border px-2 text-[11px] font-semibold ${statusTone}`}
            >
              {isProcessing && <span className="status-dot-live bg-amber" />}
              {statusLabel}
            </span>
            <span
              className="min-w-0 truncate text-[13px] font-medium text-ink"
              title={`${title} (${item.session_key})`}
            >
              {title}
            </span>
          </div>

          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            {wsShort && (
              <Badge title={`工作区完整路径：${item.workspace}`}>
                {wsShort}
              </Badge>
            )}
            <Badge title="来源工具">{clientLabel}</Badge>
            <Badge title="入队方式">{channelLabel}</Badge>
            {item.is_subagent && (
              <Badge
                className="border-accent/40 bg-accent-soft text-accent-ink"
                title="子代理会话（默认不进入常规自动批次）"
              >
                子代理{subagentInfo?.nickname ? ` · ${subagentInfo.nickname}` : ''}
                {subagentInfo?.role ? ` (${subagentInfo.role})` : ''}
              </Badge>
            )}
            {item.is_subagent && (
              <span className="text-[11px] text-ink-4">默认不进自动批次</span>
            )}
            {priorityLabel && (
              <Badge className={priorityTone(item.priority)} title="优先级">
                {priorityLabel}
              </Badge>
            )}
            {item.revision > 1 && <Badge mono>更新 v{item.revision}</Badge>}
            {item.chunk_total > 1 && (
              <Badge mono>
                分片 {item.chunk_index}/{item.chunk_total}
              </Badge>
            )}
            {item.is_oversized && item.chunk_total <= 1 && <Badge>长对话已精简</Badge>}
            {item.retry_count > 0 && (
              <Badge className="border-hot/35 bg-hot-soft text-hot" mono>
                重试 {item.retry_count}/{item.max_retries}
              </Badge>
            )}
            {item.run_id && (isProcessed || isProcessing) && (
              <button
                type="button"
                onClick={() => onJumpToRun(item.run_id as string)}
                title={`定位到批次 ${item.run_id}`}
                className="pressable mono inline-flex h-[19px] items-center rounded-[var(--radius-mark)] border border-accent/35 bg-accent-soft px-1.5 text-[11px] font-medium text-accent-ink hover:border-accent"
              >
                批次 {item.run_id.slice(-8)}
              </button>
            )}
          </div>

          {reasonLine && (
            <div
              className={`mt-1 text-[11.5px] ${
                item.status === 'dead_letter' ? 'text-hot' : 'text-ink-3'
              }`}
            >
              {reasonLine}
            </div>
          )}
        </div>

        <div className="ml-auto flex max-w-full shrink-0 flex-wrap items-center justify-end gap-1">
          {canTrigger && (
            <button
              type="button"
              onClick={onTrigger}
              disabled={triggerBusy}
              className="pressable inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink text-[11.5px] font-medium hover:bg-accent/18 disabled:opacity-40"
              title="将这段对话立即插队送入下一轮整理"
            >
              <Zap className="w-3.5 h-3.5" />
              插队整理
            </button>
          )}
          {canTrigger && (
            <button
              type="button"
              onClick={() => onAction('exclude')}
              disabled={acting}
              title="本次不整理这段对话（5秒内可撤销，也可随时重新加入）"
              className={quickBtn}
            >
              <Ban className="w-3.5 h-3.5" />
              移出队列
            </button>
          )}
          {canRequeue &&
            (isOptOut ? (
              <button
                type="button"
                onClick={() => onAction('clear_opt_out')}
                disabled={acting}
                className="pressable inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink text-[11.5px] font-medium hover:bg-accent/18 disabled:opacity-40"
              >
                <UserCheck className="w-3.5 h-3.5" />
                恢复整理
              </button>
            ) : (
              <button
                type="button"
                onClick={() => onAction('requeue')}
                disabled={acting}
                className="pressable inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink text-[11.5px] font-medium hover:bg-accent/18 disabled:opacity-40"
              >
                <RotateCcw className="w-3.5 h-3.5" />
                重新加入
              </button>
            ))}
          {canSelect && !isOptOut && (
            <button
              type="button"
              onClick={() => onAction('opt_out')}
              disabled={acting}
              title="标记后，后续同步归档也会永久跳过此会话"
              className={`${quickBtn} lg:opacity-70 lg:group-hover:opacity-100 focus-visible:opacity-100`}
            >
              <UserX className="w-3.5 h-3.5" />
              不再整理
            </button>
          )}
          <button
            type="button"
            onClick={onToggleTrail}
            aria-expanded={expanded}
            className={`${quickBtn} border border-line bg-surface`}
          >
            <ChevronDown
              className={`w-3.5 h-3.5 transition-transform duration-200 ease-[var(--ease-out-quint)] ${
                expanded ? 'rotate-180 text-accent' : ''
              }`}
            />
            轨迹
          </button>
        </div>
      </div>

      {/* Smooth interruptible trail disclosure */}
      <Collapse open={expanded} lazy>
        <div className="ml-6 mt-2.5 border-l-2 border-line pl-3 pt-0.5">
          {trailLoading ? (
            <div className="space-y-1.5 py-1" aria-busy="true">
              <div className="skeleton-block h-3.5 w-64" />
              <div className="skeleton-block h-3.5 w-48" />
            </div>
          ) : events.length === 0 ? (
            <div className="text-[11.5px] text-ink-4 py-1">暂无状态变化记录。</div>
          ) : (
            <ol className="space-y-1">
              {[...events].reverse().map((event) => (
                <li
                  key={event.seq}
                  className="flex flex-wrap items-center gap-2 text-[11.5px] text-ink-3"
                >
                  <span className="num text-ink-4" title={event.created_at}>
                    {formatRelativeTime(event.created_at)}
                  </span>
                  {event.from_status || event.to_status ? (
                    <span className="inline-flex items-center gap-1">
                      <span>
                        {labelOf(statusLabels, event.from_status, event.from_status || '—')}
                      </span>
                      <span className="text-ink-4">→</span>
                      <span className="font-medium text-ink-2">
                        {labelOf(statusLabels, event.to_status, event.to_status || '—')}
                      </span>
                    </span>
                  ) : (
                    <span className="font-medium text-ink-2">{event.event_type}</span>
                  )}
                  {event.reason_code && (
                    <span className="mono rounded-[var(--radius-mark)] bg-bg-sunk px-1 text-ink-3">
                      {labelOf(excludeReasonLabels, event.reason_code, event.reason_code)}
                    </span>
                  )}
                  {event.summary && <span>· {event.summary}</span>}
                </li>
              ))}
            </ol>
          )}
        </div>
      </Collapse>
    </div>
  )
}

interface QueueGroup {
  rootKey: string
  primaryItem: DistillQueueItem
  items: DistillQueueItem[]
  chunks: DistillQueueItem[]
  subagents: DistillQueueItem[]
}

const QueueGroupRow: React.FC<{
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

export const QueueInventoryPanel: React.FC<{
  items: DistillQueueItem[]
  dicts: Record<string, DictItem[]>
  statusFilter: string
  onStatusFilterChange: (status: string) => void
  busy: boolean
  onRefresh: () => Promise<void> | void
  onTriggerQueueIds: (queueIds: string[]) => void
  onJumpToRun: (runId: string) => void
}> = ({
  items,
  dicts,
  statusFilter,
  onStatusFilterChange,
  busy,
  onRefresh,
  onTriggerQueueIds,
  onJumpToRun,
}) => {
  const toast = useToast()
  const [query, setQuery] = useState('')
  const [clientFilter, setClientFilter] = useState('all')
  const [channelFilter, setChannelFilter] = useState('all')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [eventsByItem, setEventsByItem] = useState<Record<string, AuditEventRecord[]>>({})
  const [loadingTrailId, setLoadingTrailId] = useState<string | null>(null)
  const [actingId, setActingId] = useState<string | null>(null)
  const [bulkBusy, setBulkBusy] = useState(false)
  const [reTriage, setReTriage] = useState<QueueReTriageResult | null>(null)
  const [reTriageBusy, setReTriageBusy] = useState(false)

  const statusLabels = useMemo(
    () => dictLabels(dicts, 'queue_status', QUEUE_STATUS_LABELS),
    [dicts]
  )
  const channelLabels = useMemo(
    () => dictLabels(dicts, 'ingress_channel', INGRESS_CHANNEL_LABELS),
    [dicts]
  )
  const clientLabels = useMemo(
    () => dictLabels(dicts, 'client_type', CLIENT_LABELS),
    [dicts]
  )
  const priorityLabels = useMemo(() => dictLabels(dicts, 'priority_level', {}), [dicts])
  const excludeReasonLabels = useMemo(
    () => dictLabels(dicts, 'exclude_reason', {}),
    [dicts]
  )

  const statusToneClass = (status: string) => {
    const dictItem = (dicts.queue_status ?? []).find((entry) => entry.code === status)
    const color = String(dictItem?.meta?.color ?? STATUS_COLOR_FALLBACK[status] ?? 'gray')
    return STATUS_TONE[color] ?? STATUS_TONE.gray
  }

  const clientOptions = useMemo(
    () => Array.from(new Set(items.map((item) => item.client))).sort(),
    [items]
  )
  const channelOptions = useMemo(
    () => Array.from(new Set(items.map((item) => item.ingress_channel))).sort(),
    [items]
  )

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return items.filter((item) => {
      if (statusFilter && item.status !== statusFilter) return false
      if (clientFilter !== 'all' && item.client !== clientFilter) return false
      if (channelFilter !== 'all' && item.ingress_channel !== channelFilter) return false
      if (!needle) return true
      const haystack = [
        item.title,
        item.session_key,
        item.project,
        item.workspace,
        item.exclude_reason_detail,
        item.focus_note,
        item.run_id ?? '',
      ]
        .join(' ')
        .toLowerCase()
      return haystack.includes(needle)
    })
  }, [items, statusFilter, clientFilter, channelFilter, query])

  const itemById = useMemo(
    () => new Map(items.map((item) => [item.queue_id, item])),
    [items]
  )

  const [expandedGroupKeys, setExpandedGroupKeys] = useState<Set<string>>(new Set())

  const toggleGroupExpand = (rootKey: string) => {
    setExpandedGroupKeys((prev) => {
      const next = new Set(prev)
      if (next.has(rootKey)) next.delete(rootKey)
      else next.add(rootKey)
      return next
    })
  }

  const groups = useMemo(() => {
    const map = new Map<string, DistillQueueItem[]>()
    for (const item of filtered) {
      const rootKey = item.parent_session_key || item.session_key
      const existing = map.get(rootKey)
      if (existing) {
        existing.push(item)
      } else {
        map.set(rootKey, [item])
      }
    }
    const result: QueueGroup[] = []
    for (const [rootKey, groupItems] of map.entries()) {
      const chunks = groupItems
        .filter((i) => !i.is_subagent)
        .sort((a, b) => a.chunk_index - b.chunk_index)
      const subagents = groupItems.filter((i) => i.is_subagent)
      const primaryItem = chunks[0] || groupItems[0]
      result.push({
        rootKey,
        primaryItem,
        items: groupItems,
        chunks,
        subagents,
      })
    }
    return result
  }, [filtered])

  const pager = usePagination(groups, 10)
  const hasFilters =
    Boolean(query.trim()) ||
    Boolean(statusFilter) ||
    clientFilter !== 'all' ||
    channelFilter !== 'all'

  useEffect(() => {
    pager.resetPage()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, statusFilter, clientFilter, channelFilter])

  useEffect(() => {
    setSelected((prev) => {
      if (prev.size === 0) return prev
      const alive = new Set(
        items
          .filter((i) => i.status !== 'processed' && i.status !== 'processing')
          .map((item) => item.queue_id)
      )
      const next = new Set(Array.from(prev).filter((id) => alive.has(id)))
      return next.size === prev.size ? prev : next
    })
  }, [items])

  const toggleChecked = (queueId: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(queueId)) next.delete(queueId)
      else next.add(queueId)
      return next
    })
  }

  const toggleTrail = async (item: DistillQueueItem) => {
    if (expandedId === item.queue_id) {
      setExpandedId(null)
      return
    }
    setExpandedId(item.queue_id)
    if (eventsByItem[item.queue_id]) return
    setLoadingTrailId(item.queue_id)
    try {
      const res = await api.getQueueEvents(item.queue_id)
      setEventsByItem((prev) => ({ ...prev, [item.queue_id]: res.events }))
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '读取轨迹失败' })
      setEventsByItem((prev) => ({ ...prev, [item.queue_id]: [] }))
    } finally {
      setLoadingTrailId(null)
    }
  }

  const runItemAction = async (item: DistillQueueItem, action: QueueAction) => {
    setActingId(item.queue_id)
    try {
      if (action === 'clear_opt_out') {
        await api.queueItemAction(item.queue_id, 'clear_opt_out')
        await api.queueItemAction(item.queue_id, 'requeue')
        toast.push('已恢复整理并重新加入队列')
      } else {
        await api.queueItemAction(item.queue_id, action)
        const undoAction =
          action === 'exclude' || action === 'withdraw'
            ? async () => {
                await api.queueItemAction(item.queue_id, 'requeue')
                await onRefresh()
                toast.push('已撤销，重新放回待整理队列')
              }
            : action === 'opt_out'
              ? async () => {
                  await api.queueItemAction(item.queue_id, 'clear_opt_out')
                  await api.queueItemAction(item.queue_id, 'requeue')
                  await onRefresh()
                  toast.push('已撤销「不再整理」')
                }
              : undefined
        toast.push({
          message: `已${ACTION_LABELS[action]}：「${item.title || item.session_key}」`,
          onUndo: undoAction,
        })
      }
      setEventsByItem((prev) => {
        const next = { ...prev }
        delete next[item.queue_id]
        return next
      })
      await onRefresh()
      if (expandedId === item.queue_id) {
        const res = await api.getQueueEvents(item.queue_id)
        setEventsByItem((prev) => ({ ...prev, [item.queue_id]: res.events }))
      }
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '操作失败' })
    } finally {
      setActingId(null)
    }
  }

  const runBulkAction = async (action: QueueAction, targetIds?: string[]) => {
    const ids = targetIds ?? Array.from(selected)
    if (ids.length === 0) return
    setBulkBusy(true)
    try {
      const res = await api.queueBulkAction(ids, action)
      setSelected(new Set())
      const undo =
        action === 'exclude'
          ? async () => {
              await api.queueBulkAction(ids, 'requeue')
              await onRefresh()
              toast.push(`已撤销，重新加入 ${ids.length} 段对话`)
            }
          : undefined
      toast.push({
        message: `已批量${ACTION_LABELS[action]} ${res.updated} 段对话`,
        onUndo: undo,
      })
      await onRefresh()
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '批量操作失败' })
    } finally {
      setBulkBusy(false)
    }
  }

  const previewReTriage = async () => {
    setReTriageBusy(true)
    try {
      const res = await api.reTriageQueue(true)
      if (res.changed_count === 0) {
        setReTriage(null)
        toast.push({
          tone: 'info',
          message: `已评估 ${res.checked} 段对话，全部符合最新筛选规则，无需调整`,
        })
      } else {
        setReTriage(res)
      }
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '规则重筛失败' })
    } finally {
      setReTriageBusy(false)
    }
  }

  const applyReTriage = async () => {
    setReTriageBusy(true)
    try {
      const res = await api.reTriageQueue(false)
      setReTriage(null)
      toast.push(`已按最新规则更新 ${res.changed_count} 段对话`)
      await onRefresh()
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '应用规则重筛失败' })
    } finally {
      setReTriageBusy(false)
    }
  }

  const triggerSelected = () => {
    const ids = Array.from(selected)
    if (ids.length === 0) return
    onTriggerQueueIds(ids)
    setSelected(new Set())
  }

  const resetAllFilters = () => {
    setQuery('')
    onStatusFilterChange('')
    setClientFilter('all')
    setChannelFilter('all')
  }

  const changeTitle = (change: QueueReTriageChange) =>
    itemById.get(change.queue_id)?.title || change.session_key || change.queue_id

  const selectedItems = Array.from(selected)
    .map((id) => itemById.get(id))
    .filter((x): x is DistillQueueItem => Boolean(x))
  const queuedSelectedIds = selectedItems
    .filter((i) => i.status === 'queued')
    .map((i) => i.queue_id)
  const requeueableSelectedIds = selectedItems
    .filter(
      (i) =>
        (i.status === 'excluded' || i.status === 'withdrawn' || i.status === 'dead_letter') &&
        i.exclude_reason_code !== 'USER_OPT_OUT'
    )
    .map((i) => i.queue_id)
  const optOutSelectedIds = selectedItems
    .filter((i) => i.exclude_reason_code === 'USER_OPT_OUT')
    .map((i) => i.queue_id)

  const runBulkRestoreOptOut = async (ids: string[]) => {
    if (ids.length === 0) return
    setBulkBusy(true)
    try {
      await api.queueBulkAction(ids, 'clear_opt_out')
      const res = await api.queueBulkAction(ids, 'requeue')
      setSelected(new Set())
      toast.push(`已恢复整理并重新加入 ${res.updated} 段对话`)
      await onRefresh()
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '恢复整理失败' })
    } finally {
      setBulkBusy(false)
    }
  }

  return (
    <div className="space-y-3">
      {items.length > 0 && (
        <ListToolbar
          query={query}
          onQueryChange={setQuery}
          placeholder="搜索对话标题、项目或批次号…"
          enableSlashShortcut
          hasActiveFilters={hasFilters}
          onResetFilters={resetAllFilters}
          actions={
            <button
              type="button"
              onClick={previewReTriage}
              disabled={reTriageBusy || busy}
              className="pressable inline-flex h-8 items-center gap-1.5 rounded-[var(--radius-control)] border border-line bg-surface px-3 text-[12px] font-medium text-ink-2 hover:border-line-strong hover:text-ink disabled:opacity-50"
            >
              {reTriageBusy ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Wand2 className="w-3.5 h-3.5" />
              )}
              <span>{reTriageBusy ? '评估中…' : '按最新规则重筛'}</span>
            </button>
          }
        >
          {clientOptions.length > 1 && (
            <select
              aria-label="来源工具筛选"
              value={clientFilter}
              onChange={(e) => setClientFilter(e.target.value)}
              className="field-input h-8 px-2.5 text-[12px]"
            >
              <option value="all">全部工具</option>
              {clientOptions.map((client) => (
                <option key={client} value={client}>
                  {labelOf(clientLabels, client, client)}
                </option>
              ))}
            </select>
          )}
          {channelOptions.length > 1 && (
            <select
              aria-label="入队方式筛选"
              value={channelFilter}
              onChange={(e) => setChannelFilter(e.target.value)}
              className="field-input h-8 px-2.5 text-[12px]"
            >
              <option value="all">全部方式</option>
              {channelOptions.map((channel) => (
                <option key={channel} value={channel}>
                  {labelOf(channelLabels, channel, channel)}
                </option>
              ))}
            </select>
          )}
        </ListToolbar>
      )}

      {/* Smooth collapsible re-triage diff confirmation */}
      <Collapse open={Boolean(reTriage)}>
        {reTriage && (
          <div className="space-y-2 rounded-[var(--radius-card)] border border-amber/35 bg-amber-soft px-3.5 py-3 text-[12.5px] text-ink-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium text-ink">
                重筛预览：评估 <span className="num">{reTriage.checked}</span> 段，预计调整{' '}
                <span className="num">{reTriage.changed_count}</span> 段
                <span className="ml-2 font-normal text-ink-3">
                  （移出 <span className="num">{reTriage.would_exclude.length}</span> / 重新加入{' '}
                  <span className="num">{reTriage.would_admit.length}</span>
                  {typeof reTriage.metadata_refresh_count === 'number' && reTriage.metadata_refresh_count > 0 && (
                    <> · 刷新标题/工作区 <span className="num">{reTriage.metadata_refresh_count}</span> 项</>
                  )}
                  ）
                </span>
              </span>
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={applyReTriage}
                  disabled={reTriageBusy || reTriage.changed_count === 0}
                  className="pressable inline-flex h-7 items-center gap-1 rounded-[var(--radius-mark)] bg-accent px-2.5 text-[12px] font-medium text-accent-contrast hover:bg-accent-ink disabled:opacity-50"
                >
                  <Check className="w-3.5 h-3.5" />
                  确认应用
                </button>
                <button
                  type="button"
                  onClick={() => setReTriage(null)}
                  className="pressable inline-flex h-7 items-center gap-1 rounded-[var(--radius-mark)] border border-line bg-surface px-2.5 text-[12px] text-ink-2 hover:border-line-strong"
                >
                  <X className="w-3.5 h-3.5" />
                  关闭
                </button>
              </div>
            </div>
            {[
              { key: 'exclude', label: '将移出', rows: reTriage.would_exclude },
              { key: 'admit', label: '将重新加入', rows: reTriage.would_admit },
            ]
              .filter((group) => group.rows.length > 0)
              .map((group) => (
                <div key={group.key} className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[11.5px] text-ink-4">{group.label}：</span>
                  {group.rows.slice(0, 6).map((change) => (
                    <span
                      key={change.queue_id}
                      className="chip max-w-[260px]"
                      title={change.session_key}
                    >
                      <span className="truncate">{changeTitle(change)}</span>
                      {change.reason_code && (
                        <span className="text-ink-4">
                          · {labelOf(excludeReasonLabels, change.reason_code, change.reason_code)}
                        </span>
                      )}
                    </span>
                  ))}
                  {group.rows.length > 6 && (
                    <span className="text-[11.5px] text-ink-4">等 {group.rows.length} 段</span>
                  )}
                </div>
              ))}
          </div>
        )}
      </Collapse>

      {filtered.length === 0 ? (
        <div className="card p-10 text-center space-y-3">
          <p className="text-[13px] text-ink-4">
            {items.length === 0
              ? '目前还没有待整理对话，归档同步或手工补录后会出现在这里。'
              : '没有符合当前筛选条件的对话记录。'}
          </p>
          {hasFilters && (
            <button
              type="button"
              onClick={resetAllFilters}
              className="pressable inline-flex items-center h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] text-ink-2 hover:border-line-strong hover:text-ink"
            >
              清空筛选条件
            </button>
          )}
        </div>
      ) : (
        <div className="card overflow-hidden">
          <div className="divide-y divide-line">
            {pager.pageItems.map((group) => {
              const isGroup =
                group.items.length > 1 ||
                group.subagents.length > 0 ||
                group.chunks.length > 1
              if (isGroup) {
                return (
                  <QueueGroupRow
                    key={group.rootKey}
                    group={group}
                    expanded={expandedGroupKeys.has(group.rootKey)}
                    statusToneClass={statusToneClass}
                    statusLabels={statusLabels}
                    clientLabels={clientLabels}
                    channelLabels={channelLabels}
                    priorityLabels={priorityLabels}
                    excludeReasonLabels={excludeReasonLabels}
                    selected={selected}
                    expandedTrailId={expandedId}
                    eventsByItem={eventsByItem}
                    loadingTrailId={loadingTrailId}
                    actingId={actingId}
                    triggerBusy={busy}
                    bulkBusy={bulkBusy}
                    onToggleGroupExpand={() => toggleGroupExpand(group.rootKey)}
                    onToggleChecked={toggleChecked}
                    onToggleTrail={toggleTrail}
                    onAction={runItemAction}
                    onTriggerQueueIds={onTriggerQueueIds}
                    onBulkAction={runBulkAction}
                    onJumpToRun={onJumpToRun}
                  />
                )
              }
              const item = group.primaryItem
              return (
                <QueueRow
                  key={item.queue_id}
                  item={item}
                  statusLabel={labelOf(statusLabels, item.status, item.status)}
                  statusTone={statusToneClass(item.status)}
                  clientLabel={labelOf(clientLabels, item.client, item.client)}
                  channelLabel={labelOf(channelLabels, item.ingress_channel, item.ingress_channel)}
                  priorityLabel={labelOf(priorityLabels, item.priority, '')}
                  excludeReasonLabel={labelOf(
                    excludeReasonLabels,
                    item.exclude_reason_code,
                    item.exclude_reason_code || '规则筛选'
                  )}
                  statusLabels={statusLabels}
                  excludeReasonLabels={excludeReasonLabels}
                  checked={selected.has(item.queue_id)}
                  expanded={expandedId === item.queue_id}
                  events={eventsByItem[item.queue_id] ?? []}
                  trailLoading={loadingTrailId === item.queue_id}
                  acting={actingId === item.queue_id}
                  triggerBusy={busy}
                  onToggleChecked={() => toggleChecked(item.queue_id)}
                  onToggleTrail={() => toggleTrail(item)}
                  onAction={(action) => runItemAction(item, action)}
                  onTrigger={() => onTriggerQueueIds([item.queue_id])}
                  onJumpToRun={onJumpToRun}
                />
              )
            })}
          </div>
          <div className="px-4 pb-3">
            <PaginationBar
              total={pager.total}
              page={pager.page}
              pageSize={pager.pageSize}
              totalPages={pager.totalPages}
              onPageChange={pager.setPage}
              onPageSizeChange={pager.setPageSize}
            />
          </div>
        </div>
      )}

      {/* Apple-style Floating Bulk Action Island (portaled to body, zero table layout shift!) */}
      {typeof document !== 'undefined' &&
        createPortal(
          <div
            aria-live="polite"
            className={`fixed left-1/2 bottom-[72px] lg:bottom-6 z-40 -translate-x-1/2 transition-[opacity,transform] duration-[220ms] ease-[var(--ease-out-quint)] ${
              selected.size > 0
                ? 'opacity-100 translate-y-0 pointer-events-auto'
                : 'opacity-0 translate-y-4 pointer-events-none'
            }`}
          >
            <div className="flex flex-wrap items-center gap-2 rounded-full border border-line-strong bg-surface/95 backdrop-blur-md px-3.5 py-2 shadow-[var(--shadow-sheet)]">
              <span className="text-[12.5px] font-medium text-ink px-1">
                已选 <span className="num font-semibold text-accent">{selected.size}</span> 段
              </span>
              <span className="h-4 w-px bg-line" />
              {queuedSelectedIds.length > 0 && (
                <button
                  type="button"
                  onClick={triggerSelected}
                  disabled={busy || bulkBusy}
                  className="pressable inline-flex h-7 items-center gap-1 rounded-full bg-accent px-3 text-[12px] font-medium text-accent-contrast hover:bg-accent-ink disabled:opacity-50"
                >
                  <Zap className="w-3.5 h-3.5" />
                  插队整理
                </button>
              )}
              {requeueableSelectedIds.length > 0 && (
                <button
                  type="button"
                  onClick={() => runBulkAction('requeue', requeueableSelectedIds)}
                  disabled={bulkBusy}
                  className="pressable inline-flex h-7 items-center gap-1 rounded-full border border-line bg-bg-sunk px-2.5 text-[12px] text-ink-2 hover:border-line-strong hover:text-ink disabled:opacity-50"
                >
                  <RotateCcw className="w-3.5 h-3.5" />
                  重新加入
                </button>
              )}
              {optOutSelectedIds.length > 0 && (
                <button
                  type="button"
                  onClick={() => runBulkRestoreOptOut(optOutSelectedIds)}
                  disabled={bulkBusy}
                  className="pressable inline-flex h-7 items-center gap-1 rounded-full border border-line bg-bg-sunk px-2.5 text-[12px] text-ink-2 hover:border-line-strong hover:text-ink disabled:opacity-50"
                >
                  <UserCheck className="w-3.5 h-3.5" />
                  恢复整理
                </button>
              )}
              {queuedSelectedIds.length > 0 && (
                <button
                  type="button"
                  onClick={() => runBulkAction('exclude', queuedSelectedIds)}
                  disabled={bulkBusy}
                  className="pressable inline-flex h-7 items-center gap-1 rounded-full border border-line bg-bg-sunk px-2.5 text-[12px] text-ink-2 hover:border-line-strong hover:text-ink disabled:opacity-50"
                >
                  <Ban className="w-3.5 h-3.5" />
                  移出队列
                </button>
              )}
              <button
                type="button"
                onClick={() => setSelected(new Set())}
                aria-label="取消选择"
                className="pressable-sm inline-flex size-7 items-center justify-center rounded-full text-ink-4 hover:bg-bg-sunk hover:text-ink"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>,
          document.body
        )}
    </div>
  )
}
