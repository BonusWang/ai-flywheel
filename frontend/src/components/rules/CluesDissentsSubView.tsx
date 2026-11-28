import React, { useMemo, useState } from 'react'
import { ChevronRight, GitBranch } from 'lucide-react'
import { DISSENT_STATUS_LABELS, formatDateYMD, labelOf } from '../../lib/labels'
import type { DissentRecord } from '../../types'
import { ListToolbar, PaginationBar, usePagination } from '../ListControls'
import { StatusBadge } from '../StatusBadge'
import { Collapse } from '../ui/Collapse'
import { DISSENT_TARGET_LABELS, byCreatedDesc } from './cluesUtils'

interface Props {
  dissents: DissentRecord[]
  busy: boolean
  expandedIds: Record<string, boolean>
  onToggleExpand: (id: string) => void
  onDissentStatus: (dissentId: string, status: 'merged' | 'dismissed') => void
  onBulkDissents: (ids: string[], status: 'merged' | 'dismissed') => Promise<void> | void
}

export const CluesDissentsSubView: React.FC<Props> = ({
  dissents,
  busy,
  expandedIds,
  onToggleExpand: toggleExpand,
  onDissentStatus: handleDissentStatus,
  onBulkDissents: onBulkDissentsProp,
}) => {
  const [query, setQuery] = useState('')
  const [dissentStatus, setDissentStatus] = useState<string>('open')
  const [selectedDissents, setSelectedDissents] = useState<string[]>([])

  const filteredDissents = useMemo(() => {
    const q = query.trim().toLowerCase()
    return dissents
      .filter((d) => (dissentStatus === 'all' ? true : d.status === dissentStatus))
      .filter((d) =>
        !q
          ? true
          : `${d.counter_claim} ${d.condition_scope} ${d.target_title || ''} ${d.target_id} ${d.evidence_pointer}`
              .toLowerCase()
              .includes(q)
      )
      .sort((a, b) => byCreatedDesc(a.created_at, b.created_at))
  }, [dissents, dissentStatus, query])

  const dissentPager = usePagination(filteredDissents, 10)

  const toggleSelect = (
    id: string,
    list: string[],
    setter: React.Dispatch<React.SetStateAction<string[]>>
  ) => {
    setter(list.includes(id) ? list.filter((x) => x !== id) : [...list, id])
  }

  const handleBulkDissents = async (ids: string[], status: 'merged' | 'dismissed') => {
    await onBulkDissentsProp(ids, status)
    setSelectedDissents([])
  }

  return (
  <section key="sub-dissents" className="animate-tab-enter card p-5 space-y-3.5">
    <div className="rounded-[var(--radius-tile)] bg-amber-soft/50 border border-amber/25 px-4 py-2.5 text-[12px] text-ink-2 flex items-center gap-2">
      <GitBranch className="w-4 h-4 text-amber-ink shrink-0" />
      <span>
        <strong className="text-ink">规则分歧与反例</strong>
        ：既有规则在特定场景不适用时保留反例不直接覆盖。已补充进规则请点「已纳入规则」，无效反例请点「忽略」。
      </span>
    </div>

    <ListToolbar
      query={query}
      onQueryChange={(q) => {
        setQuery(q)
        dissentPager.resetPage()
      }}
      placeholder="搜索反例内容、适用边界或关联规则…"
      enableSlashShortcut
      hasActiveFilters={Boolean(query.trim()) || dissentStatus !== 'open'}
      onResetFilters={() => {
        setQuery('')
        setDissentStatus('open')
        dissentPager.resetPage()
      }}
      actions={
        selectedDissents.length > 0 ? (
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              disabled={busy}
              onClick={() => handleBulkDissents(selectedDissents, 'merged')}
              className="pressable h-8 px-2.5 rounded-[var(--radius-control)] bg-ok-soft text-ok-ink text-[12px] font-medium"
            >
              批量纳入规则 ({selectedDissents.length})
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => handleBulkDissents(selectedDissents, 'dismissed')}
              className="pressable h-8 px-2.5 rounded-[var(--radius-control)] border border-line bg-surface text-ink-3 text-[12px] hover:text-ink"
            >
              批量忽略
            </button>
          </div>
        ) : null
      }
    >
      <select
        aria-label="分歧状态筛选"
        value={dissentStatus}
        onChange={(e) => {
          setDissentStatus(e.target.value)
          setSelectedDissents([])
          dissentPager.resetPage()
        }}
        className="field-input h-8 px-2.5 text-[12px]"
      >
        <option value="open">仅看待处理</option>
        <option value="all">全部状态</option>
        <option value="merged">已纳入规则</option>
        <option value="dismissed">已忽略</option>
      </select>
    </ListToolbar>

    {filteredDissents.length === 0 ? (
      <p className="text-[13px] text-ink-4 py-8 text-center">当前没有符合条件的规则分歧记录。</p>
    ) : (
      <div className="divide-y divide-line-soft">
        {dissentPager.pageItems.map((d) => {
          const key = `dis:${d.dissent_id}`
          const expanded = Boolean(expandedIds[key])
          const checked = selectedDissents.includes(d.dissent_id)
          return (
            <div key={d.dissent_id} className="py-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-start gap-2 min-w-0 flex-1">
                  {d.status === 'open' && (
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() =>
                        toggleSelect(d.dissent_id, selectedDissents, setSelectedDissents)
                      }
                      className="mt-1 size-3.5 accent-[var(--accent)] cursor-pointer"
                    />
                  )}
                  <button
                    type="button"
                    aria-expanded={expanded}
                    onClick={() => toggleExpand(key)}
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
                        <span
                          className={`px-1.5 py-0.5 rounded text-[11px] font-medium ${
                            d.status === 'open'
                              ? 'bg-amber-soft text-amber-ink'
                              : d.status === 'merged'
                                ? 'bg-ok-soft text-ok-ink'
                                : 'bg-bg-sunk text-ink-4'
                          }`}
                        >
                          {DISSENT_STATUS_LABELS[d.status] || d.status}
                        </span>
                        <span className="chip truncate max-w-[300px]">
                          {DISSENT_TARGET_LABELS[d.target_type] || d.target_type}：
                          {d.target_title || d.target_id}
                        </span>
                        <span className="num text-[11.5px] text-ink-4">
                          {formatDateYMD(d.created_at)}
                        </span>
                      </div>
                      <div className="text-[13.5px] font-medium text-ink line-clamp-1">
                        {d.counter_claim}
                      </div>
                    </div>
                  </button>
                </div>
                {d.status === 'open' && (
                  <div className="flex items-center gap-1.5 shrink-0">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => handleDissentStatus(d.dissent_id, 'merged')}
                      className="pressable h-7 px-2.5 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink text-[12px] font-medium disabled:opacity-50"
                    >
                      已纳入规则
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => handleDissentStatus(d.dissent_id, 'dismissed')}
                      className="pressable h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[12px] hover:border-line-strong hover:text-ink disabled:opacity-50"
                    >
                      忽略
                    </button>
                  </div>
                )}
              </div>
              <Collapse open={expanded} lazy>
                <div className="ml-8 mt-2 rounded-[var(--radius-tile)] bg-bg-sunk/60 border border-line-soft p-3 text-[12.5px] space-y-1.5">
                  <p className="text-ink-2">
                    <span className="text-ink-4">完整反例：</span>
                    {d.counter_claim}
                  </p>
                  {d.condition_scope && (
                    <p className="text-ink-3">
                      <span className="text-ink-4">适用条件：</span>
                      {d.condition_scope}
                    </p>
                  )}
                  <div className="flex flex-wrap gap-3 text-[11.5px] text-ink-4 mono">
                    <span>目标 ID：{d.target_id}</span>
                    {d.evidence_pointer && <span>证据：{d.evidence_pointer}</span>}
                    {d.session_key && <span>会话：{d.session_key}</span>}
                  </div>
                </div>
              </Collapse>
            </div>
          )
        })}
      </div>
    )}

    <PaginationBar
      total={dissentPager.total}
      page={dissentPager.page}
      pageSize={dissentPager.pageSize}
      totalPages={dissentPager.totalPages}
      onPageChange={dissentPager.setPage}
      onPageSizeChange={dissentPager.setPageSize}
    />
  </section>
  )
}
