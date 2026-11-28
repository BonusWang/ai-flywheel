import React, { useMemo } from 'react'
import {
  Ban,
  Check,
  ChevronDown,
  Loader2,
  RotateCcw,
  UserCheck,
  UserX,
  X,
  Zap,
} from 'lucide-react'
import { formatRelativeTime, labelOf } from '../../lib/labels'
import type { AuditEventRecord, DistillQueueItem, QueueAction } from '../../types'
import { Collapse } from '../ui/Collapse'

export const STATUS_COLOR_FALLBACK: Record<string, string> = {
  queued: 'blue',
  processing: 'amber',
  processed: 'green',
  excluded: 'gray',
  withdrawn: 'gray',
  dead_letter: 'red',
}

export const STATUS_TONE: Record<string, string> = {
  blue: 'border-accent/35 bg-accent-soft text-accent-ink',
  amber: 'border-amber/40 bg-amber-soft text-amber-ink',
  green: 'border-ok/35 bg-ok-soft text-ok-ink',
  gray: 'border-line-strong bg-bg-sunk text-ink-3',
  red: 'border-hot/40 bg-hot-soft text-hot',
}

export const NEUTRAL_BADGE = 'border-line bg-bg-sunk text-ink-3'

export const ACTION_LABELS: Record<QueueAction, string> = {
  requeue: '重新加入',
  exclude: '移出队列',
  withdraw: '撤回',
  opt_out: '不再整理',
  clear_opt_out: '恢复整理',
}


export const Badge: React.FC<{
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


export function priorityTone(priority: string): string {
  if (priority === 'P0_CORE') return 'border-hot bg-hot text-white'
  if (priority === 'P1_SIGNAL') return 'border-amber/40 bg-amber-soft text-amber-ink'
  return NEUTRAL_BADGE
}


/** One compact, scannable queue row with contextual actions and smooth collapsible trail. */
export const QueueRow: React.FC<{
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
