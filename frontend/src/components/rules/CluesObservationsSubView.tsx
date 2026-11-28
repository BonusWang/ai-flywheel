import React, { useMemo, useState } from 'react'
import { Archive, ArrowUpRight, ChevronRight } from 'lucide-react'
import { formatDateYMD } from '../../lib/labels'
import type { MistakeObservation } from '../../types'
import { ListToolbar, PaginationBar, usePagination } from '../ListControls'
import { StatusBadge } from '../StatusBadge'
import { Collapse } from '../ui/Collapse'
import { ConfirmButton } from '../ui/ConfirmButton'
import { byCreatedDesc } from './cluesUtils'

interface Props {
  observations: MistakeObservation[]
  busy: boolean
  expandedIds: Record<string, boolean>
  onToggleExpand: (id: string) => void
  onPromote: (obsId: string) => void
  onArchive: (obsId: string) => void
  onBulkArchive: (ids: string[]) => Promise<boolean>
}

export const CluesObservationsSubView: React.FC<Props> = ({
  observations,
  busy,
  expandedIds,
  onToggleExpand: toggleExpand,
  onPromote: handlePromoteObservation,
  onArchive: handleArchiveObservation,
  onBulkArchive,
}) => {
  const [query, setQuery] = useState('')
  const [obsStatus, setObsStatus] = useState<string>('observing')
  const [obsSeverity, setObsSeverity] = useState<string>('all')
  const [selectedObs, setSelectedObs] = useState<string[]>([])

  const filteredObs = useMemo(() => {
    const q = query.trim().toLowerCase()
    return observations
      .filter((o) => (obsStatus === 'all' ? true : o.status === obsStatus))
      .filter((o) => (obsSeverity === 'all' ? true : o.severity_grade === obsSeverity))
      .filter((o) =>
        !q
          ? true
          : `${o.title} ${o.root_cause_key} ${o.thinking_root_cause} ${o.truth}`
              .toLowerCase()
              .includes(q)
      )
      .sort((a, b) => byCreatedDesc(a.created_at, b.created_at))
  }, [observations, obsStatus, obsSeverity, query])

  const obsPager = usePagination(filteredObs, 10)

  const toggleSelect = (
    id: string,
    list: string[],
    setter: React.Dispatch<React.SetStateAction<string[]>>
  ) => {
    setter(list.includes(id) ? list.filter((x) => x !== id) : [...list, id])
  }

  const handleBulkArchiveObs = async (ids: string[]) => {
    const ok = await onBulkArchive(ids)
    if (ok) setSelectedObs([])
  }

  return (
  <section key="sub-obs" className="animate-tab-enter card p-5 space-y-3.5">
    <div className="rounded-[var(--radius-tile)] bg-bg-sunk/65 border border-line-soft px-4 py-2.5 text-[12px] text-ink-3">
      <strong className="text-ink">系统自动流转（事不过三）</strong>
      ：普通失误自动按根因计数，满 3 次自动晋升规则，超 30 天未复发的低危错题由周五精简自动归档。
    </div>

    <ListToolbar
      query={query}
      onQueryChange={(q) => {
        setQuery(q)
        obsPager.resetPage()
      }}
      placeholder="搜索错题标题、根因或做法…"
      enableSlashShortcut
      hasActiveFilters={Boolean(query.trim()) || obsStatus !== 'observing' || obsSeverity !== 'all'}
      onResetFilters={() => {
        setQuery('')
        setObsStatus('observing')
        setObsSeverity('all')
        obsPager.resetPage()
      }}
      actions={
        selectedObs.length > 0 ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => handleBulkArchiveObs(selectedObs)}
            className="pressable inline-flex items-center gap-1 h-8 px-2.5 rounded-[var(--radius-control)] border border-line bg-surface text-[12px] font-medium text-ink hover:border-accent"
          >
            <Archive className="w-3.5 h-3.5" />
            批量归档 ({selectedObs.length})
          </button>
        ) : null
      }
    >
      <select
        aria-label="错题状态筛选"
        value={obsStatus}
        onChange={(e) => {
          setObsStatus(e.target.value)
          setSelectedObs([])
          obsPager.resetPage()
        }}
        className="field-input h-8 px-2.5 text-[12px]"
      >
        <option value="observing">仅看观察中</option>
        <option value="all">全部状态</option>
        <option value="promoted">已升规则</option>
        <option value="archived">已归档</option>
      </select>
      <select
        aria-label="错题级别筛选"
        value={obsSeverity}
        onChange={(e) => {
          setObsSeverity(e.target.value)
          setSelectedObs([])
          obsPager.resetPage()
        }}
        className="field-input h-8 px-2.5 text-[12px]"
      >
        <option value="all">全部级别</option>
        <option value="red_silent">漏检失误</option>
        <option value="yellow_caught">自查拦截</option>
        <option value="green_minor">轻微</option>
      </select>
    </ListToolbar>

    {filteredObs.length === 0 ? (
      <p className="text-[13px] text-ink-4 py-8 text-center">没有符合条件的错题记录。</p>
    ) : (
      <div className="divide-y divide-line-soft">
        {obsPager.pageItems.map((obs) => {
          const key = `obs:${obs.obs_id}`
          const expanded = Boolean(expandedIds[key])
          const checked = selectedObs.includes(obs.obs_id)
          return (
            <div key={obs.obs_id} className="py-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-start gap-2 min-w-0 flex-1">
                  {obs.status === 'observing' && (
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleSelect(obs.obs_id, selectedObs, setSelectedObs)}
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
                        <StatusBadge status={obs.severity_grade} />
                        <StatusBadge status={obs.status} />
                        <span className="chip num">复现 {obs.occurrence_count}/3</span>
                        <span className="num text-[11.5px] text-ink-4">
                          {formatDateYMD(obs.created_at)}
                        </span>
                      </div>
                      <div className="text-[13.5px] font-semibold text-ink line-clamp-1">
                        {obs.title}
                      </div>
                    </div>
                  </button>
                </div>
                {obs.status === 'observing' && (
                  <div className="flex items-center gap-1.5 shrink-0">
                    {obs.severity_grade === 'red_silent' || obs.occurrence_count >= 3 ? (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => handlePromoteObservation(obs.obs_id)}
                        className="pressable inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink text-[12px] font-medium hover:bg-accent/18 disabled:opacity-50"
                      >
                        <ArrowUpRight className="w-3.5 h-3.5" />
                        升为规则
                      </button>
                    ) : (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => handlePromoteObservation(obs.obs_id)}
                        className="pressable h-7 px-2 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[11.5px] hover:border-line-strong hover:text-ink disabled:opacity-50"
                      >
                        提前升规则
                      </button>
                    )}
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => handleArchiveObservation(obs.obs_id)}
                      className="pressable h-7 px-2 rounded-[var(--radius-mark)] border border-line text-ink-4 text-[11.5px] hover:border-line-strong hover:text-ink disabled:opacity-50"
                    >
                      归档
                    </button>
                  </div>
                )}
              </div>
              <Collapse open={expanded} lazy>
                <div className="ml-8 mt-2 rounded-[var(--radius-tile)] bg-bg-sunk/60 border border-line-soft p-3 text-[12.5px] space-y-1.5">
                  <div className="mono text-[11.5px] text-ink-4">
                    根因标识：{obs.root_cause_key}
                  </div>
                  {obs.thinking_root_cause && (
                    <p className="text-ink-2">
                      <span className="text-ink-4">思维根因：</span>
                      {obs.thinking_root_cause}
                    </p>
                  )}
                  {obs.truth && (
                    <p className="text-ink-3">
                      <span className="text-ink-4">正确做法：</span>
                      {obs.truth}
                    </p>
                  )}
                </div>
              </Collapse>
            </div>
          )
        })}
      </div>
    )}

    <PaginationBar
      total={obsPager.total}
      page={obsPager.page}
      pageSize={obsPager.pageSize}
      totalPages={obsPager.totalPages}
      onPageChange={obsPager.setPage}
      onPageSizeChange={obsPager.setPageSize}
    />
  </section>
  )
}
