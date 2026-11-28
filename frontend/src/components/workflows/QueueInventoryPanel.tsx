import React, { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  Ban,
  Check,
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
import { QueueGroupRow, type QueueGroup } from './QueueGroupRow'
import {
  ACTION_LABELS,
  Badge,
  QueueRow,
  STATUS_COLOR_FALLBACK,
  STATUS_TONE,
} from './QueueRow'

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
