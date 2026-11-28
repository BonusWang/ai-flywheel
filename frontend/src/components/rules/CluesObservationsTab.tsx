import React, { useEffect, useMemo, useState } from 'react'
import {
  Archive,
  ArrowUpRight,
  CheckCheck,
  ChevronRight,
  ShieldAlert,
} from 'lucide-react'
import { api } from '../../api'
import { ListToolbar, PaginationBar, usePagination } from '../ListControls'
import { PillTabs } from '../PageLayout'
import { StatusBadge } from '../StatusBadge'
import { Collapse } from '../ui/Collapse'
import { ConfirmButton } from '../ui/ConfirmButton'
import { useToast } from '../ui/Toast'
import { formatDateYMD, labelOf } from '../../lib/labels'
import type { DissentRecord, MistakeObservation, RetroItem } from '../../types'
import { CluesDissentsSubView } from './CluesDissentsSubView'
import { CluesObservationsSubView } from './CluesObservationsSubView'
import { CluesRetrosSubView } from './CluesRetrosSubView'
import {
  DISSENT_TARGET_LABELS,
  byCreatedDesc,
  canonicalRetroKey,
  type ActionItem,
  type RetroGroup,
  type SubViewKey,
} from './cluesUtils'

export const CluesObservationsTab: React.FC<{
  observations: MistakeObservation[]
  dissents: DissentRecord[]
  retros: RetroItem[]
  initialSubView?: SubViewKey
  intentNonce?: number
  onChanged: () => Promise<void>
}> = ({ observations, dissents, retros, initialSubView, intentNonce, onChanged }) => {
  const toast = useToast()
  const [subView, setSubView] = useState<SubViewKey>(initialSubView || 'action')
  const [expandedIds, setExpandedIds] = useState<Record<string, boolean>>({})
  const [busy, setBusy] = useState(false)
  const [query, setQuery] = useState('')

  useEffect(() => {
    if (!intentNonce || !initialSubView) return
    setSubView(initialSubView)
  }, [intentNonce, initialSubView])

  const toggleExpand = (id: string) =>
    setExpandedIds((prev) => ({ ...prev, [id]: !prev[id] }))

  const switchSubView = (next: SubViewKey) => {
    setSubView(next)
    setQuery('')
  }

  const highPriorityObs = useMemo(
    () =>
      observations
        .filter(
          (o) =>
            o.status === 'observing' &&
            (o.severity_grade === 'red_silent' || o.occurrence_count >= 3)
        )
        .sort((a, b) => byCreatedDesc(a.created_at, b.created_at)),
    [observations]
  )

  const openDissents = useMemo(
    () =>
      dissents
        .filter((d) => d.status === 'open')
        .sort((a, b) => byCreatedDesc(a.created_at, b.created_at)),
    [dissents]
  )

  const observingCount = useMemo(
    () => observations.filter((o) => o.status === 'observing').length,
    [observations]
  )

  const openRetroGroupCount = useMemo(() => {
    const keys = new Set<string>()
    for (const r of retros) {
      if (r.status === 'open' || r.status === 'in_review') {
        keys.add(canonicalRetroKey(r))
      }
    }
    return keys.size
  }, [retros])

  const actionItems = useMemo<ActionItem[]>(() => {
    const q = query.trim().toLowerCase()
    const list: ActionItem[] = [
      ...highPriorityObs.map((obs) => ({
        kind: 'mistake' as const,
        id: `m:${obs.obs_id}`,
        createdAt: obs.created_at || obs.updated_at,
        obs,
      })),
      ...openDissents.map((dissent) => ({
        kind: 'dissent' as const,
        id: `d:${dissent.dissent_id}`,
        createdAt: dissent.created_at || dissent.updated_at,
        dissent,
      })),
    ].sort((a, b) => byCreatedDesc(a.createdAt, b.createdAt))

    if (!q) return list
    return list.filter((item) => {
      if (item.kind === 'mistake') {
        const o = item.obs
        return `${o.title} ${o.root_cause_key} ${o.thinking_root_cause} ${o.truth}`
          .toLowerCase()
          .includes(q)
      }
      const d = item.dissent
      return `${d.counter_claim} ${d.condition_scope} ${d.target_title || ''} ${d.target_id}`
        .toLowerCase()
        .includes(q)
    })
  }, [highPriorityObs, openDissents, query])

  const actionPager = usePagination(actionItems, 10)

  const runAction = async (fn: () => Promise<void>) => {
    setBusy(true)
    try {
      await fn()
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : '操作失败，请稍后重试'
      toast.push({ tone: 'error', message })
    } finally {
      setBusy(false)
    }
  }

  const handlePromoteObservation = (obsId: string) =>
    runAction(async () => {
      const res = await api.promoteObservation(obsId)
      await onChanged()
      toast.push(`已升级为试运行规则：${res.rule.title}`)
    })

  const handleArchiveObservation = (obsId: string) =>
    runAction(async () => {
      await api.archiveObservation(obsId)
      await onChanged()
      toast.push({
        message: '已归档该观察错题',
        onUndo: async () => {
          await api.restoreObservation(obsId)
          await onChanged()
          toast.push('已撤销归档，放回错题观察池')
        },
      })
    })

  const handleBulkArchiveObs = (ids: string[]) =>
    runAction(async () => {
      if (ids.length === 0) return
      const res = await api.bulkArchiveObservations(ids)
      await onChanged()
      toast.push({
        message: `已批量归档 ${res.updated} 条观察错题`,
        onUndo: async () => {
          await api.bulkRestoreObservations(ids)
          await onChanged()
          toast.push(`已撤销，恢复 ${ids.length} 条错题`)
        },
      })
    })

  const handleDissentStatus = (dissentId: string, status: 'merged' | 'dismissed') =>
    runAction(async () => {
      await api.updateDissentStatus(dissentId, status)
      await onChanged()
      toast.push({
        message: status === 'merged' ? '已标记为纳入规则' : '已忽略该规则分歧',
        onUndo: async () => {
          await api.updateDissentStatus(dissentId, 'open')
          await onChanged()
          toast.push('已撤销，恢复为待处理分歧')
        },
      })
    })

  const handleBulkDissents = (ids: string[], status: 'merged' | 'dismissed') =>
    runAction(async () => {
      if (ids.length === 0) return
      const res = await api.bulkUpdateDissents(ids, status)
      await onChanged()
      toast.push({
        message: `已批量${status === 'merged' ? '纳入规则' : '忽略'} ${res.updated} 条分歧`,
        onUndo: async () => {
          await api.bulkUpdateDissents(ids, 'open')
          await onChanged()
          toast.push(`已撤销，恢复 ${ids.length} 条分歧`)
        },
      })
    })

  const handleRetroGroupStatus = (ids: string[], status: RetroItem['status']) =>
    runAction(async () => {
      if (ids.length === 0) return
      const res = await api.bulkUpdateRetro(ids, status)
      await onChanged()
      toast.push({
        message: `已标记 ${res.updated} 条线索为${status === 'resolved' ? '已处理' : '已忽略'}`,
        onUndo: async () => {
          await api.bulkUpdateRetro(ids, 'open')
          await onChanged()
          toast.push(`已撤销，恢复 ${ids.length} 条线索`)
        },
      })
    })

  const handlePromoteRetroGroup = (ids: string[]) =>
    runAction(async () => {
      if (ids.length === 0) return
      const res = await api.promoteRetros(ids)
      await onChanged()
      toast.push(`已将 ${res.resolved_count} 条线索转入错题观察池并结案`)
    })

  const handleBulkPromoteRetroGroups = (groups: RetroGroup[]) =>
    runAction(async () => {
      const targets = groups.filter((g) => g.ids.length > 0)
      if (targets.length === 0) return
      let resolvedTotal = 0
      for (const group of targets) {
        const res = await api.promoteRetros(group.ids)
        resolvedTotal += res.resolved_count
      }
      await onChanged()
      toast.push(`已将 ${targets.length} 组（共 ${resolvedTotal} 条）线索转入错题观察池并结案`)
    })

  const totalActionCount = highPriorityObs.length + openDissents.length

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <PillTabs
          items={[
            { key: 'action', label: '待我拍板', count: totalActionCount },
            { key: 'observations', label: '错题观察池', count: observingCount },
            { key: 'dissents', label: '规则分歧', count: openDissents.length },
            { key: 'retros', label: '实时异常线索', count: openRetroGroupCount },
          ]}
          active={subView}
          onChange={(k) => switchSubView(k as SubViewKey)}
        />
      </div>

      {/* ==================== 1. 待我拍板 ==================== */}
      {subView === 'action' && (
        <section key="sub-action" className="animate-tab-enter card p-5 space-y-3.5">
          <div className="rounded-[var(--radius-tile)] bg-accent-softer/70 border border-accent/20 px-4 py-2.5 flex flex-wrap items-center justify-between gap-2 text-[12.5px]">
            <div className="flex items-center gap-2 text-ink-2">
              <ShieldAlert className="w-4 h-4 text-accent shrink-0" />
              <span>
                只汇总真正需要你拍板的高优项：
                <strong className="text-ink font-semibold"> 漏检失误 ({highPriorityObs.length})</strong>{' '}
                与 <strong className="text-ink font-semibold">待处理分歧 ({openDissents.length})</strong>
                ；普通观察错题与运行线索已由系统自动流转。
              </span>
            </div>
          </div>

          {actionItems.length > 0 && (
            <ListToolbar
              query={query}
              onQueryChange={(q) => {
                setQuery(q)
                actionPager.resetPage()
              }}
              placeholder="搜索高优错题或规则分歧…"
              enableSlashShortcut
            />
          )}

          {actionItems.length === 0 ? (
            <div className="py-10 text-center text-[13px] text-ink-4">
              当前没有需要人工拍板的高优错题或分歧，系统正在后台自动观察与流转。
            </div>
          ) : (
            <div className="divide-y divide-line-soft">
              {actionPager.pageItems.map((item) => {
                const expanded = Boolean(expandedIds[item.id])
                if (item.kind === 'mistake') {
                  const obs = item.obs
                  return (
                    <div key={item.id} className="py-3">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <button
                          type="button"
                          aria-expanded={expanded}
                          onClick={() => toggleExpand(item.id)}
                          className="flex items-start gap-2 min-w-0 flex-1 text-left group"
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
                              <span className="px-1.5 py-0.5 rounded bg-hot-soft text-hot text-[11px] font-semibold">
                                错题 · 需拍板
                              </span>
                              <StatusBadge status={obs.severity_grade} />
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
                        <div className="flex items-center gap-1.5 shrink-0">
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => handlePromoteObservation(obs.obs_id)}
                            title="将其升级为试运行避坑规则，下次同类任务自动提醒"
                            className="pressable inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink text-[12px] font-medium hover:bg-accent/20 disabled:opacity-50"
                          >
                            <ArrowUpRight className="w-3.5 h-3.5" />
                            升为规则
                          </button>
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => handleArchiveObservation(obs.obs_id)}
                            title="归档为一次性噪音（5秒内可撤销）"
                            className="pressable h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[12px] hover:border-line-strong hover:text-ink disabled:opacity-50"
                          >
                            归档
                          </button>
                        </div>
                      </div>
                      <Collapse open={expanded} lazy>
                        <div className="ml-6 mt-2 rounded-[var(--radius-tile)] bg-bg-sunk/60 border border-line-soft p-3 text-[12.5px] space-y-1.5">
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
                }

                const d = item.dissent
                return (
                  <div key={item.id} className="py-3">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <button
                        type="button"
                        aria-expanded={expanded}
                        onClick={() => toggleExpand(item.id)}
                        className="flex items-start gap-2 min-w-0 flex-1 text-left group"
                      >
                        <ChevronRight
                          className={`w-4 h-4 shrink-0 mt-0.5 transition-transform duration-200 ease-[var(--ease-out-quint)] ${
                            expanded ? 'rotate-90 text-accent' : 'text-ink-4 group-hover:text-ink'
                          }`}
                        />
                        <div className="min-w-0 flex-1 space-y-1">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="px-1.5 py-0.5 rounded bg-amber-soft text-amber-ink text-[11px] font-semibold">
                              分歧 · 待处理
                            </span>
                            <span className="chip truncate max-w-[280px]">
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
                    </div>
                    <Collapse open={expanded} lazy>
                      <div className="ml-6 mt-2 rounded-[var(--radius-tile)] bg-bg-sunk/60 border border-line-soft p-3 text-[12.5px] space-y-1.5">
                        <p className="text-ink-2">
                          <span className="text-ink-4">反例说明：</span>
                          {d.counter_claim}
                        </p>
                        {d.condition_scope && (
                          <p className="text-ink-3">
                            <span className="text-ink-4">适用边界：</span>
                            {d.condition_scope}
                          </p>
                        )}
                        <div className="flex flex-wrap gap-3 text-[11.5px] text-ink-4 mono">
                          <span>目标：{d.target_id}</span>
                          {d.evidence_pointer && <span>来源：{d.evidence_pointer}</span>}
                        </div>
                      </div>
                    </Collapse>
                  </div>
                )
              })}
            </div>
          )}

          <PaginationBar
            total={actionPager.total}
            page={actionPager.page}
            pageSize={actionPager.pageSize}
            totalPages={actionPager.totalPages}
            onPageChange={actionPager.setPage}
            onPageSizeChange={actionPager.setPageSize}
          />
        </section>
      )}


      {subView === 'observations' && (
        <CluesObservationsSubView
          observations={observations}
          busy={busy}
          expandedIds={expandedIds}
          onToggleExpand={toggleExpand}
          onPromote={handlePromoteObservation}
          onArchive={handleArchiveObservation}
          onBulkArchive={handleBulkArchiveObs}
        />
      )}

      {subView === 'dissents' && (
        <CluesDissentsSubView
          dissents={dissents}
          busy={busy}
          expandedIds={expandedIds}
          onToggleExpand={toggleExpand}
          onDissentStatus={handleDissentStatus}
          onBulkDissents={handleBulkDissents}
        />
      )}

      {subView === 'retros' && (
        <CluesRetrosSubView
          retros={retros}
          busy={busy}
          expandedIds={expandedIds}
          onToggleExpand={toggleExpand}
          onPromoteGroup={handlePromoteRetroGroup}
          onBulkPromoteGroups={handleBulkPromoteRetroGroups}
          onGroupStatus={handleRetroGroupStatus}
        />
      )}
    </div>
  )
}
