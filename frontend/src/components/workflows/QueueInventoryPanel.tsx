import React, { useEffect, useMemo, useState } from 'react'
import {
  Ban,
  Check,
  ChevronDown,
  ChevronUp,
  RotateCcw,
  Undo2,
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

/** The six inventory statuses, in the order the toolbar lists them. */
const STATUS_ORDER = [
  'queued',
  'processing',
  'processed',
  'excluded',
  'withdrawn',
  'dead_letter',
] as const

/** Colour hints the builtin dictionary seeds carry for each status. */
const STATUS_COLOR_FALLBACK: Record<string, string> = {
  queued: 'blue',
  processing: 'amber',
  processed: 'green',
  excluded: 'gray',
  withdrawn: 'gray',
  dead_letter: 'red',
}

/** Dictionary colour name -> the design system's badge classes. */
const STATUS_TONE: Record<string, string> = {
  blue: 'border-accent/35 bg-accent-soft text-accent-ink',
  amber: 'border-amber/40 bg-amber-soft text-amber-ink',
  green: 'border-ok/35 bg-ok-soft text-ok-ink',
  gray: 'border-line-strong bg-bg-sunk text-ink-3',
  red: 'border-hot/40 bg-hot-soft text-hot',
}

const NEUTRAL_BADGE = 'border-line bg-bg-sunk text-ink-3'

/** The Chinese verb shown in the confirmation line after an intervention. */
const ACTION_LABELS: Record<QueueAction, string> = {
  requeue: '捞回入队',
  exclude: '排除',
  withdraw: '撤回',
  opt_out: '标为不沉淀',
  clear_opt_out: '解除不沉淀',
}

/** A small fixed-height badge; the tone classes override the neutral default. */
const Badge: React.FC<{
  children: React.ReactNode
  className?: string
  mono?: boolean
  title?: string
}> = ({ children, className = '', mono = false, title }) => (
  <span
    title={title}
    className={`inline-flex h-[19px] max-w-[280px] items-center truncate rounded-[var(--radius-mark)] border px-1.5 text-[11px] font-medium ${
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

/** One compact, scannable queue row with its contextual interventions. */
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
  onToggleChecked,
  onToggleTrail,
  onAction,
  onTrigger,
  onJumpToRun,
}) => {
  const isOptOut = item.exclude_reason_code === 'USER_OPT_OUT'
  const canTrigger = item.status === 'queued'
  const canRequeue =
    item.status === 'excluded' ||
    item.status === 'withdrawn' ||
    item.status === 'dead_letter'
  const title = item.title || item.session_key

  const quickBtn =
    'inline-flex items-center gap-1 h-6 px-1.5 rounded-[var(--radius-mark)] text-[11.5px] text-ink-3 hover:text-ink hover:bg-bg-sunk disabled:opacity-40'

  let reasonLine = ''
  if (item.status === 'excluded') {
    reasonLine = `排除原因：${excludeReasonLabel}${
      item.exclude_reason_detail ? ` · ${item.exclude_reason_detail}` : ''
    }`
  } else if (item.status === 'dead_letter') {
    reasonLine = `死信原因：${item.last_error || '重试耗尽，等待人工排查'}`
  }

  return (
    <div
      className={`px-4 py-3 transition-colors ${
        checked ? 'bg-accent-softer' : 'hover:bg-bg-sunk/40'
      }`}
    >
      <div className="flex flex-wrap items-start gap-3">
        <input
          type="checkbox"
          aria-label={`选择 ${title}`}
          checked={checked}
          onChange={onToggleChecked}
          className="mt-1 size-3.5 shrink-0 accent-accent"
        />

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span
              className={`inline-flex h-[20px] items-center rounded-full border px-2 text-[11px] font-semibold ${statusTone}`}
            >
              {statusLabel}
            </span>
            <span
              className="min-w-0 truncate text-[13px] font-medium text-ink"
              title={title}
            >
              {title}
            </span>
            <span
              className="mono max-w-[240px] truncate text-[11px] text-ink-4"
              title={item.session_key}
            >
              {item.session_key}
            </span>
          </div>

          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <Badge title="接入工具">{clientLabel}</Badge>
            <Badge title="入队通道">{channelLabel}</Badge>
            <Badge className={priorityTone(item.priority)} title="优先级">
              {priorityLabel}
            </Badge>
            {item.revision > 1 && <Badge mono>rev {item.revision}</Badge>}
            {item.chunk_total > 1 && (
              <Badge mono>
                分片 {item.chunk_index}/{item.chunk_total}
              </Badge>
            )}
            {item.is_oversized && item.chunk_total <= 1 && <Badge>超大瘦身</Badge>}
            {item.retry_count > 0 && (
              <Badge className="border-hot/35 bg-hot-soft text-hot" mono>
                重试 {item.retry_count}/{item.max_retries}
              </Badge>
            )}
            {item.run_id && (item.status === 'processed' || item.status === 'processing') && (
              <button
                type="button"
                onClick={() => onJumpToRun(item.run_id as string)}
                title={`跳转到批次 ${item.run_id}`}
                className="mono inline-flex h-[19px] items-center rounded-[var(--radius-mark)] border border-accent/35 bg-accent-soft px-1.5 text-[11px] font-medium text-accent-ink hover:border-accent"
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

        <div className="ml-auto flex max-w-full shrink-0 flex-wrap items-center justify-end gap-1 sm:max-w-[380px]">
          {canTrigger && (
            <button
              type="button"
              onClick={onTrigger}
              disabled={triggerBusy}
              className={quickBtn}
              title="把这一条插队交给下一轮流水线立即沉淀"
            >
              <Zap className="w-3.5 h-3.5" />
              插队沉淀
            </button>
          )}
          {canTrigger && (
            <button
              type="button"
              onClick={() => onAction('exclude')}
              disabled={acting}
              className={quickBtn}
            >
              <Ban className="w-3.5 h-3.5" />
              排除
            </button>
          )}
          {canTrigger && (
            <button
              type="button"
              onClick={() => onAction('withdraw')}
              disabled={acting}
              className={quickBtn}
            >
              <Undo2 className="w-3.5 h-3.5" />
              撤回
            </button>
          )}
          {canRequeue &&
            (isOptOut ? (
              <button
                type="button"
                onClick={() => onAction('clear_opt_out')}
                disabled={acting}
                className={quickBtn}
              >
                <UserCheck className="w-3.5 h-3.5" />
                解除不沉淀并入队
              </button>
            ) : (
              <button
                type="button"
                onClick={() => onAction('requeue')}
                disabled={acting}
                className={quickBtn}
              >
                <RotateCcw className="w-3.5 h-3.5" />
                捞回入队
              </button>
            ))}
          {!isOptOut && (
            <button
              type="button"
              onClick={() => onAction('opt_out')}
              disabled={acting}
              className={quickBtn}
            >
              <UserX className="w-3.5 h-3.5" />
              标为不沉淀
            </button>
          )}
          <button
            type="button"
            onClick={onToggleTrail}
            aria-expanded={expanded}
            className={`${quickBtn} border border-line bg-surface`}
          >
            {expanded ? (
              <ChevronUp className="w-3.5 h-3.5" />
            ) : (
              <ChevronDown className="w-3.5 h-3.5" />
            )}
            轨迹
          </button>
        </div>
      </div>

      {expanded && (
        <div className="ml-6 mt-2.5 border-l-2 border-line pl-3">
          {trailLoading ? (
            <div className="text-[11.5px] text-ink-4">正在读取状态轨迹…</div>
          ) : events.length === 0 ? (
            <div className="text-[11.5px] text-ink-4">暂无状态轨迹记录。</div>
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
                  <span className="text-ink-4">· {event.actor}</span>
                  {event.summary && <span>· {event.summary}</span>}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * The distill-queue inventory panel: filters, bulk interventions, rule
 * re-triage preview and the per-row state-transition trail.
 *
 * Every label comes from the unified dictionary (`/api/site/dicts`) with the
 * local `labels.ts` tables as the offline fallback, so a badge never falls
 * back to a raw enum code the user cannot read.
 */
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
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

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

  const pager = usePagination(filtered, 10)

  useEffect(() => {
    pager.resetPage()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, statusFilter, clientFilter, channelFilter])

  useEffect(() => {
    setSelected((prev) => {
      if (prev.size === 0) return prev
      const alive = new Set(items.map((item) => item.queue_id))
      const next = new Set(Array.from(prev).filter((id) => alive.has(id)))
      return next.size === prev.size ? prev : next
    })
  }, [items])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(null), 6000)
    return () => window.clearTimeout(timer)
  }, [notice])

  const toggleChecked = (queueId: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(queueId)) {
        next.delete(queueId)
      } else {
        next.add(queueId)
      }
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
      setError(e.message || '状态轨迹读取失败')
      setEventsByItem((prev) => ({ ...prev, [item.queue_id]: [] }))
    } finally {
      setLoadingTrailId(null)
    }
  }

  const runItemAction = async (item: DistillQueueItem, action: QueueAction) => {
    setActingId(item.queue_id)
    setError(null)
    try {
      if (action === 'clear_opt_out') {
        // Clearing the session flag alone leaves the row excluded: the button
        // promises "解除不沉淀并入队", so the item is requeued right after.
        await api.queueItemAction(item.queue_id, 'clear_opt_out')
        await api.queueItemAction(item.queue_id, 'requeue')
        setNotice('已解除不沉淀并捞回入队 1 条。')
      } else {
        await api.queueItemAction(item.queue_id, action)
        setNotice(`已对 1 条队列项执行「${ACTION_LABELS[action]}」。`)
      }
      // The intervention just appended an audit event: drop the cached trail
      // so reopening it shows the new transition instead of a stale one.
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
      setError(e.message || '队列操作失败')
    } finally {
      setActingId(null)
    }
  }

  const runBulkAction = async (action: QueueAction) => {
    const ids = Array.from(selected)
    if (ids.length === 0) return
    setBulkBusy(true)
    setError(null)
    try {
      const res = await api.queueBulkAction(ids, action)
      setSelected(new Set())
      setNotice(`批量「${ACTION_LABELS[action]}」已完成，更新 ${res.updated} 条。`)
      await onRefresh()
    } catch (e: any) {
      setError(e.message || '批量操作失败')
    } finally {
      setBulkBusy(false)
    }
  }

  const previewReTriage = async () => {
    setReTriageBusy(true)
    setError(null)
    try {
      const res = await api.reTriageQueue(true)
      setReTriage(res)
    } catch (e: any) {
      setError(e.message || '规则重筛预览失败')
    } finally {
      setReTriageBusy(false)
    }
  }

  const applyReTriage = async () => {
    setReTriageBusy(true)
    setError(null)
    try {
      const res = await api.reTriageQueue(false)
      setReTriage(null)
      setNotice(`规则重筛已应用：更新 ${res.changed_count} 条队列项。`)
      await onRefresh()
    } catch (e: any) {
      setError(e.message || '规则重筛应用失败')
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

  const changeTitle = (change: QueueReTriageChange) =>
    itemById.get(change.queue_id)?.title || change.session_key || change.queue_id

  return (
    <div className="space-y-3">
      <ListToolbar
        query={query}
        onQueryChange={setQuery}
        placeholder="按标题、会话 key、项目或批次 ID 搜索…"
        sortHint="按入队时间倒序"
        actions={
          <button
            type="button"
            onClick={previewReTriage}
            disabled={reTriageBusy || busy}
            className="inline-flex h-8 items-center gap-1.5 rounded-[var(--radius-control)] border border-line bg-surface px-3 text-[12px] font-medium text-ink-2 hover:border-line-strong hover:text-ink disabled:opacity-50"
          >
            <Wand2 className="w-3.5 h-3.5" />
            {reTriageBusy ? '重筛中…' : '预览规则重筛'}
          </button>
        }
      >
        <select
          aria-label="库存状态筛选"
          value={statusFilter}
          onChange={(e) => onStatusFilterChange(e.target.value)}
          className="h-8 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 text-[12px] text-ink"
        >
          <option value="">全部状态</option>
          {STATUS_ORDER.map((status) => (
            <option key={status} value={status}>
              {labelOf(statusLabels, status, status)}
            </option>
          ))}
        </select>
        <select
          aria-label="接入工具筛选"
          value={clientFilter}
          onChange={(e) => setClientFilter(e.target.value)}
          className="h-8 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 text-[12px] text-ink"
        >
          <option value="all">全部接入工具</option>
          {clientOptions.map((client) => (
            <option key={client} value={client}>
              {labelOf(clientLabels, client, client)}
            </option>
          ))}
        </select>
        <select
          aria-label="入队通道筛选"
          value={channelFilter}
          onChange={(e) => setChannelFilter(e.target.value)}
          className="h-8 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 text-[12px] text-ink"
        >
          <option value="all">全部通道</option>
          {channelOptions.map((channel) => (
            <option key={channel} value={channel}>
              {labelOf(channelLabels, channel, channel)}
            </option>
          ))}
        </select>
      </ListToolbar>

      {error && (
        <div className="rounded-[var(--radius-card)] border border-hot/30 bg-hot-soft px-3.5 py-2.5 text-[12.5px] text-hot">
          {error}
        </div>
      )}
      {notice && (
        <div className="rounded-[var(--radius-card)] border border-ok/30 bg-ok-soft px-3.5 py-2.5 text-[12.5px] text-ok-ink">
          {notice}
        </div>
      )}

      {reTriage && (
        <div className="space-y-2 rounded-[var(--radius-card)] border border-amber/35 bg-amber-soft px-3.5 py-3 text-[12.5px] text-ink-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-medium text-ink">
              规则重筛预览：评估 <span className="num">{reTriage.checked}</span> 条，预计变化{' '}
              <span className="num">{reTriage.changed_count}</span> 条
              <span className="ml-2 font-normal text-ink-3">
                （将排除 <span className="num">{reTriage.would_exclude.length}</span> 条 /
                将捞回 <span className="num">{reTriage.would_admit.length}</span> 条）
              </span>
            </span>
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                onClick={applyReTriage}
                disabled={reTriageBusy || reTriage.changed_count === 0}
                className="inline-flex h-7 items-center gap-1 rounded-[var(--radius-mark)] bg-accent px-2.5 text-[12px] font-medium text-accent-contrast hover:bg-accent-ink disabled:opacity-50"
              >
                <Check className="w-3.5 h-3.5" />
                应用重筛
              </button>
              <button
                type="button"
                onClick={() => setReTriage(null)}
                className="inline-flex h-7 items-center gap-1 rounded-[var(--radius-mark)] border border-line bg-surface px-2.5 text-[12px] text-ink-2 hover:border-line-strong"
              >
                <X className="w-3.5 h-3.5" />
                关闭
              </button>
            </div>
          </div>
          {[
            { key: 'exclude', label: '将排除', rows: reTriage.would_exclude },
            { key: 'admit', label: '将捞回', rows: reTriage.would_admit },
          ]
            .filter((group) => group.rows.length > 0)
            .map((group) => (
              <div key={group.key} className="flex flex-wrap items-center gap-1.5">
                <span className="text-[11.5px] text-ink-4">{group.label}：</span>
                {group.rows.slice(0, 6).map((change) => (
                  <span
                    key={change.queue_id}
                    className="chip max-w-[280px]"
                    title={change.session_key}
                  >
                    <span className="truncate">{changeTitle(change)}</span>
                    {change.reason_code && (
                      <span className="text-ink-4">
                        ·{' '}
                        {labelOf(excludeReasonLabels, change.reason_code, change.reason_code)}
                      </span>
                    )}
                  </span>
                ))}
                {group.rows.length > 6 && (
                  <span className="text-[11.5px] text-ink-4">等 {group.rows.length} 条</span>
                )}
              </div>
            ))}
        </div>
      )}

      {selected.size > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-[var(--radius-card)] border border-accent/30 bg-accent-soft px-3.5 py-2.5">
          <span className="text-[12.5px] text-accent-ink">
            已选 <span className="num font-semibold">{selected.size}</span> 条队列项
          </span>
          <div className="flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              onClick={triggerSelected}
              disabled={busy || bulkBusy}
              className="inline-flex h-7 items-center gap-1 rounded-[var(--radius-mark)] bg-accent px-2.5 text-[12px] font-medium text-accent-contrast hover:bg-accent-ink disabled:opacity-50"
            >
              <Zap className="w-3.5 h-3.5" />
              插队立即沉淀 ({selected.size})
            </button>
            <button
              type="button"
              onClick={() => runBulkAction('requeue')}
              disabled={bulkBusy}
              className="inline-flex h-7 items-center gap-1 rounded-[var(--radius-mark)] border border-line bg-surface px-2.5 text-[12px] text-ink-2 hover:border-line-strong disabled:opacity-50"
            >
              <RotateCcw className="w-3.5 h-3.5" />
              批量捞回入队
            </button>
            <button
              type="button"
              onClick={() => runBulkAction('exclude')}
              disabled={bulkBusy}
              className="inline-flex h-7 items-center gap-1 rounded-[var(--radius-mark)] border border-line bg-surface px-2.5 text-[12px] text-ink-2 hover:border-line-strong disabled:opacity-50"
            >
              <Ban className="w-3.5 h-3.5" />
              批量排除
            </button>
            <button
              type="button"
              onClick={() => setSelected(new Set())}
              className="inline-flex h-7 items-center gap-1 rounded-[var(--radius-mark)] border border-transparent px-2 text-[12px] text-ink-3 hover:text-ink"
            >
              <X className="w-3.5 h-3.5" />
              清空选择
            </button>
          </div>
        </div>
      )}

      {filtered.length === 0 ? (
        <div className="card p-10 text-center text-[13px] text-ink-4">
          {items.length === 0
            ? '库存为空：归档同步或手工补录后，对话会先进入沉淀队列。'
            : '没有符合当前筛选条件的队列项。'}
        </div>
      ) : (
        <div className="card overflow-hidden">
          <div className="divide-y divide-line">
            {pager.pageItems.map((item) => (
              <QueueRow
                key={item.queue_id}
                item={item}
                statusLabel={labelOf(statusLabels, item.status, item.status)}
                statusTone={statusToneClass(item.status)}
                clientLabel={labelOf(clientLabels, item.client, item.client)}
                channelLabel={labelOf(channelLabels, item.ingress_channel, item.ingress_channel)}
                priorityLabel={labelOf(priorityLabels, item.priority, item.priority)}
                excludeReasonLabel={labelOf(
                  excludeReasonLabels,
                  item.exclude_reason_code,
                  item.exclude_reason_code || '未标注'
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
            ))}
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
    </div>
  )
}
