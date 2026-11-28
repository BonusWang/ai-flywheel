import React, { useMemo, useState } from 'react'
import { ArrowUpRight, CheckCheck, ChevronRight, EyeOff, Radar } from 'lucide-react'
import { CLIENT_LABELS, RETRO_KIND_LABELS, formatDateYMD, labelOf } from '../../lib/labels'
import type { RetroItem } from '../../types'
import { ListToolbar, PaginationBar, usePagination } from '../ListControls'
import { StatusBadge } from '../StatusBadge'
import { Collapse } from '../ui/Collapse'
import { ConfirmButton } from '../ui/ConfirmButton'
import { groupRetroItems, type RetroGroup } from './cluesUtils'

interface Props {
  retros: RetroItem[]
  busy: boolean
  expandedIds: Record<string, boolean>
  onToggleExpand: (id: string) => void
  onPromoteGroup: (ids: string[]) => Promise<void> | void
  onBulkPromoteGroups: (groups: RetroGroup[]) => Promise<void> | void
  onGroupStatus: (ids: string[], status: RetroItem['status']) => Promise<void> | void
}

export const CluesRetrosSubView: React.FC<Props> = ({
  retros,
  busy,
  expandedIds,
  onToggleExpand: toggleExpand,
  onPromoteGroup,
  onBulkPromoteGroups,
  onGroupStatus,
}) => {
  const [query, setQuery] = useState('')
  const [retroStatus, setRetroStatus] = useState<string>('open')
  const [selectedRetroKeys, setSelectedRetroKeys] = useState<string[]>([])

  const groupedRetros = useMemo(
    () => groupRetroItems(retros, retroStatus, query),
    [retros, retroStatus, query]
  )

  const suggestedNoiseIds = useMemo(
    () =>
      groupedRetros
        .filter((g) => g.openCount > 0 && g.advisory?.verdict === 'ignore')
        .flatMap((g) => g.ids),
    [groupedRetros]
  )

  const retroPager = usePagination(groupedRetros, 10)

  const toggleSelect = (
    id: string,
    list: string[],
    setter: React.Dispatch<React.SetStateAction<string[]>>
  ) => {
    setter(list.includes(id) ? list.filter((x) => x !== id) : [...list, id])
  }

  const handleRetroGroupStatus = async (ids: string[], status: RetroItem['status']) => {
    await onGroupStatus(ids, status)
    setSelectedRetroKeys([])
  }

  const handlePromoteRetroGroup = async (ids: string[]) => {
    await onPromoteGroup(ids)
    setSelectedRetroKeys([])
  }

  const handleBulkPromoteRetroGroups = async (groups: RetroGroup[]) => {
    await onBulkPromoteGroups(groups)
    setSelectedRetroKeys([])
  }

  return (
  <section key="sub-retros" className="animate-tab-enter card p-5 space-y-3.5">
    <div className="rounded-[var(--radius-tile)] bg-bg-sunk/65 border border-line-soft px-4 py-2.5 text-[12px] text-ink-3 flex items-center gap-2">
      <Radar className="w-4 h-4 text-accent shrink-0" />
      <span>
        <strong className="text-ink">系统自动聚合的运行线索</strong>
        ：由工具在执行期实时上报。若关联会话在待提炼队列中，随下次沉淀自动结算；无关联待审会话的线索需人工处置：高价值线索可「转入错题观察池」沉淀，无复用价值可标「已人工处理」或「忽略」。
      </span>
    </div>

    <ListToolbar
      query={query}
      onQueryChange={(q) => {
        setQuery(q)
        retroPager.resetPage()
      }}
      placeholder="搜索异常摘要、工具名或客户端…"
      enableSlashShortcut
      hasActiveFilters={Boolean(query.trim()) || retroStatus !== 'open'}
      onResetFilters={() => {
        setQuery('')
        setRetroStatus('open')
        retroPager.resetPage()
      }}
      actions={
        <div className="flex flex-wrap items-center gap-1.5">
          {selectedRetroKeys.length > 0 && (
            <>
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  const groups = groupedRetros.filter((g) =>
                    selectedRetroKeys.includes(g.key)
                  )
                  handleBulkPromoteRetroGroups(groups)
                }}
                className="pressable inline-flex items-center gap-1 h-8 px-2.5 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12px] font-medium hover:bg-accent-ink"
              >
                <ArrowUpRight className="w-3.5 h-3.5" />
                批量转入错题池
              </button>
              <button
                type="button"
                disabled={busy}
                title="仅标记已处理并结案，不再送入下次沉淀"
                onClick={() => {
                  const ids = groupedRetros
                    .filter((g) => selectedRetroKeys.includes(g.key))
                    .flatMap((g) => g.ids)
                  handleRetroGroupStatus(ids, 'resolved')
                }}
                className="pressable inline-flex items-center gap-1 h-8 px-2.5 rounded-[var(--radius-control)] bg-ok-soft text-ok-ink text-[12px] font-medium"
              >
                <CheckCheck className="w-3.5 h-3.5" />
                批量已处理
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  const ids = groupedRetros
                    .filter((g) => selectedRetroKeys.includes(g.key))
                    .flatMap((g) => g.ids)
                  handleRetroGroupStatus(ids, 'dismissed')
                }}
                className="pressable inline-flex items-center gap-1 h-8 px-2.5 rounded-[var(--radius-control)] border border-line bg-surface text-ink-3 text-[12px] hover:text-ink"
              >
                <EyeOff className="w-3.5 h-3.5" />
                批量忽略
              </button>
            </>
          )}
          {retroStatus === 'open' && suggestedNoiseIds.length > 0 && (
            <button
              type="button"
              disabled={busy}
              onClick={() => handleRetroGroupStatus(suggestedNoiseIds, 'dismissed')}
              className="pressable h-8 px-2.5 rounded-[var(--radius-control)] border border-line bg-bg-sunk text-ink-2 text-[12px] font-medium hover:border-line-strong hover:text-ink"
            >
              一键忽略建议噪音 ({suggestedNoiseIds.length})
            </button>
          )}
          {retroStatus === 'open' && groupedRetros.length > 0 && (
            <ConfirmButton
              label="忽略本页全部"
              confirmLabel={`确认忽略本页 ${retroPager.pageItems.length} 组？`}
              disabled={busy}
              onConfirm={() =>
                handleRetroGroupStatus(
                  retroPager.pageItems.flatMap((g) => g.ids),
                  'dismissed'
                )
              }
              className="h-8 px-2.5 rounded-[var(--radius-control)] border border-line bg-surface text-ink-3 text-[12px] hover:border-hot/50 hover:text-hot disabled:opacity-50"
              confirmClassName="h-8 px-2.5 rounded-[var(--radius-control)] border border-hot bg-hot text-white text-[12px] font-semibold"
            />
          )}
        </div>
      }
    >
      <select
        aria-label="线索状态筛选"
        value={retroStatus}
        onChange={(e) => {
          setRetroStatus(e.target.value)
          setSelectedRetroKeys([])
          retroPager.resetPage()
        }}
        className="field-input h-8 px-2.5 text-[12px]"
      >
        <option value="open">仅看待处理</option>
        <option value="all">全部（含历史）</option>
        <option value="resolved">已处理</option>
        <option value="dismissed">已忽略</option>
      </select>
    </ListToolbar>

    {groupedRetros.length === 0 ? (
      <p className="text-[13px] text-ink-4 py-8 text-center">暂无符合条件的实时异常线索。</p>
    ) : (
      <div className="divide-y divide-line-soft">
        {retroPager.pageItems.map((group) => {
          const item = group.latest
          const expanded = Boolean(expandedIds[group.key])
          const isOpen = item.status === 'open' || item.status === 'in_review'
          const checked = selectedRetroKeys.includes(group.key)
          return (
            <div key={group.key} className="py-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-start gap-2 min-w-0 flex-1">
                  {isOpen && (
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() =>
                        toggleSelect(group.key, selectedRetroKeys, setSelectedRetroKeys)
                      }
                      className="mt-1 size-3.5 accent-[var(--accent)] cursor-pointer"
                    />
                  )}
                  <button
                    type="button"
                    aria-expanded={expanded}
                    onClick={() => toggleExpand(group.key)}
                    className="flex items-start gap-1.5 min-w-0 flex-1 text-left group"
                  >
                    <ChevronRight
                      className={`w-4 h-4 shrink-0 mt-0.5 transition-transform duration-200 ease-[var(--ease-out-quint)] ${
                        expanded
                          ? 'rotate-90 text-accent'
                          : 'text-ink-4 group-hover:text-ink'
                      }`}
                    />
                    <div className="min-w-0 flex-1 space-y-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <StatusBadge status={item.status} />
                        {isOpen && (
                          group.hasQueuedSession ? (
                            <span
                              className="px-1.5 py-0.5 rounded bg-ok-soft text-ok-ink text-[11px] font-medium cursor-help"
                              title="关联会话当前在待提炼队列中，下次批次 P05 结算时将自动结案"
                            >
                              随下次沉淀自动结算
                            </span>
                          ) : (
                            <span
                              className="px-1.5 py-0.5 rounded bg-bg-sunk border border-line-soft text-ink-3 text-[11px] font-medium cursor-help"
                              title="关联会话不在待提炼队列中（如未归档或独立上报），不会自动结算，需人工确认处理、转错题或忽略"
                            >
                              无关联待审会话
                            </span>
                          )
                        )}
                        <span className="chip">{labelOf(RETRO_KIND_LABELS, item.kind)}</span>
                        {group.clients.map((c) => (
                          <span key={c} className="chip">
                            {labelOf(CLIENT_LABELS, c)}
                          </span>
                        ))}
                        {group.count > 1 && (
                          <span className="px-1.5 py-0.5 rounded bg-amber-soft text-amber-ink text-[11px] font-semibold num">
                            同类 ×{group.count}
                          </span>
                        )}
                        {group.maxAttempt > 0 && (
                          <span className="px-1.5 py-0.5 rounded bg-bg-sunk border border-line-soft text-ink-3 text-[11px] num">
                            最高连试 {group.maxAttempt} 次
                          </span>
                        )}
                        <span className="num text-[11.5px] text-ink-4">
                          最近 {formatDateYMD(item.created_at)}
                        </span>
                      </div>
                      <p className="text-[13px] text-ink line-clamp-1">
                        {group.advisory?.normalized_summary || item.summary}
                      </p>
                      {group.advisory && (
                        <div className="flex flex-wrap items-center gap-1.5 text-[11.5px]">
                          <span
                            className={
                              group.advisory.verdict === 'keep'
                                ? 'px-1.5 py-0.5 rounded bg-accent-soft text-accent font-medium'
                                : group.advisory.verdict === 'ignore'
                                  ? 'px-1.5 py-0.5 rounded bg-bg-sunk border border-line-soft text-ink-3 font-medium'
                                  : 'px-1.5 py-0.5 rounded bg-amber-soft text-amber-ink font-medium'
                            }
                          >
                            {group.advisory.verdict_label}
                          </span>
                          <span className="text-ink-3">{group.advisory.reason}</span>
                        </div>
                      )}
                    </div>
                  </button>
                </div>
                {isOpen && (
                  <div className="flex items-center gap-1.5 shrink-0">
                    {group.advisory?.verdict === 'keep' ? (
                      <>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => handlePromoteRetroGroup(group.ids)}
                          className="pressable inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-accent text-accent-contrast text-[12px] font-medium hover:bg-accent-ink disabled:opacity-50"
                        >
                          <ArrowUpRight className="w-3 h-3" />
                          {group.count > 1 ? `整组转入错题池 (${group.count})` : '转入错题观察池'}
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          title="仅标记已处理并结案，不再送入下次沉淀"
                          onClick={() => handleRetroGroupStatus(group.ids, 'resolved')}
                          className="pressable h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[12px] hover:text-ink disabled:opacity-50"
                        >
                          {group.count > 1 ? `整组已处理 (${group.count})` : '已人工处理'}
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => handleRetroGroupStatus(group.ids, 'dismissed')}
                          className="pressable h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[12px] hover:text-ink disabled:opacity-50"
                        >
                          忽略
                        </button>
                      </>
                    ) : group.advisory?.verdict === 'ignore' ? (
                      <>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => handleRetroGroupStatus(group.ids, 'dismissed')}
                          className="pressable h-7 px-2.5 rounded-[var(--radius-mark)] bg-bg-sunk border border-line-strong text-ink text-[12px] font-medium hover:bg-surface disabled:opacity-50"
                        >
                          {group.count > 1 ? `整组忽略 (${group.count})` : '忽略'}
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          title="仅标记已处理并结案，不再送入下次沉淀"
                          onClick={() => handleRetroGroupStatus(group.ids, 'resolved')}
                          className="pressable h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[12px] hover:text-ink disabled:opacity-50"
                        >
                          已人工处理
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => handlePromoteRetroGroup(group.ids)}
                          className="pressable inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] border border-line bg-surface text-ink text-[12px] font-medium hover:border-accent hover:text-accent disabled:opacity-50"
                        >
                          <ArrowUpRight className="w-3 h-3" />
                          转入错题观察池
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          title="仅标记已处理并结案，不再送入下次沉淀"
                          onClick={() => handleRetroGroupStatus(group.ids, 'resolved')}
                          className="pressable h-7 px-2.5 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink text-[12px] font-medium disabled:opacity-50"
                        >
                          {group.count > 1 ? `整组已处理 (${group.count})` : '已人工处理'}
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => handleRetroGroupStatus(group.ids, 'dismissed')}
                          className="pressable h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[12px] hover:text-ink disabled:opacity-50"
                        >
                          忽略
                        </button>
                      </>
                    )}
                  </div>
                )}
              </div>
              <Collapse open={expanded} lazy>
                <div className="ml-8 mt-2 rounded-[var(--radius-tile)] bg-bg-sunk/60 border border-line-soft p-3 text-[12px] text-ink-3 space-y-1.5">
                  <p className="text-ink-2">{item.summary}</p>
                  {group.advisory?.matched_preference && (
                    <p className="text-[11.5px] text-accent">
                      关联个人红线：{group.advisory.matched_preference}
                    </p>
                  )}
                  <div className="flex flex-wrap gap-3 text-[11.5px] text-ink-4 mono">
                    {item.tool_name && <span>工具：{item.tool_name}</span>}
                    <span>合并条数：{group.count}</span>
                    {group.maxAttempt > 0 && <span>最高连续尝试：{group.maxAttempt} 次</span>}
                    {group.sessions.length > 0 && (
                      <span>涉及会话：{group.sessions.slice(0, 3).join(', ')}</span>
                    )}
                  </div>
                </div>
              </Collapse>
            </div>
          )
        })}
      </div>
    )}

    <PaginationBar
      total={retroPager.total}
      page={retroPager.page}
      pageSize={retroPager.pageSize}
      totalPages={retroPager.totalPages}
      onPageChange={retroPager.setPage}
      onPageSizeChange={retroPager.setPageSize}
    />
  </section>
  )
}
