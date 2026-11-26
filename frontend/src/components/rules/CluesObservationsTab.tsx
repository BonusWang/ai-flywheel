import React, { useEffect, useMemo, useState } from 'react'
import {
  Archive,
  ArrowUpRight,
  CheckCheck,
  ChevronRight,
  EyeOff,
  GitBranch,
  Radar,
  ShieldAlert,
} from 'lucide-react'
import { api } from '../../api'
import { ListToolbar, PaginationBar, usePagination } from '../ListControls'
import { PillTabs } from '../PageLayout'
import { StatusBadge } from '../StatusBadge'
import { Collapse } from '../ui/Collapse'
import { ConfirmButton } from '../ui/ConfirmButton'
import { useToast } from '../ui/Toast'
import {
  CLIENT_LABELS,
  DISSENT_STATUS_LABELS,
  formatDateYMD,
  labelOf,
  RETRO_KIND_LABELS,
} from '../../lib/labels'
import type { DissentRecord, MistakeObservation, RetroAdvisory, RetroItem } from '../../types'

type SubViewKey = 'action' | 'observations' | 'dissents' | 'retros'

type ActionItem =
  | { kind: 'mistake'; id: string; createdAt: string; obs: MistakeObservation }
  | { kind: 'dissent'; id: string; createdAt: string; dissent: DissentRecord }

interface RetroGroup {
  key: string
  ids: string[]
  latest: RetroItem
  count: number
  openCount: number
  maxAttempt: number
  clients: string[]
  sessions: string[]
  advisory?: RetroAdvisory
  hasQueuedSession: boolean
}

function canonicalRetroKey(item: RetroItem): string {
  if (item.advisory?.group_key) return item.advisory.group_key
  const tool = (item.tool_name || '').trim().toLowerCase()
  const norm = item.summary.trim().replace(/已连续尝试\s*\d+\s*次/g, '已连续尝试 N 次')
  if (item.kind === 'retry_loop' && tool) return `retry_loop::${tool}`
  return `${item.kind}::${norm}`
}

function extractAttempt(item: RetroItem): number {
  if (item.advisory?.attempt && item.advisory.attempt > 0) return item.advisory.attempt
  const raw = item.detail?.attempt
  if (typeof raw === 'number' && raw > 0) return raw
  const m = item.summary.match(/已连续尝试\s*(\d+)\s*次/)
  return m ? Number(m[1]) : 0
}

const DISSENT_TARGET_LABELS: Record<string, string> = {
  rule: '规则',
  skill: 'Skill',
  knowledge_doc: '知识笔记',
}

function byCreatedDesc(a?: string | null, b?: string | null): number {
  return String(b || '').localeCompare(String(a || ''))
}

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

  // Filters per sub-view
  const [query, setQuery] = useState('')
  const [obsStatus, setObsStatus] = useState<string>('observing')
  const [obsSeverity, setObsSeverity] = useState<string>('all')
  const [dissentStatus, setDissentStatus] = useState<string>('open')
  const [retroStatus, setRetroStatus] = useState<string>('open')

  // Multi-select state per sub-view
  const [selectedObs, setSelectedObs] = useState<string[]>([])
  const [selectedDissents, setSelectedDissents] = useState<string[]>([])
  const [selectedRetroKeys, setSelectedRetroKeys] = useState<string[]>([])

  useEffect(() => {
    if (!intentNonce || !initialSubView) return
    setSubView(initialSubView)
  }, [intentNonce, initialSubView])

  const toggleExpand = (id: string) =>
    setExpandedIds((prev) => ({ ...prev, [id]: !prev[id] }))

  const switchSubView = (next: SubViewKey) => {
    setSubView(next)
    setQuery('')
    setSelectedObs([])
    setSelectedDissents([])
    setSelectedRetroKeys([])
  }

  // ---- Derived datasets (all default created_at DESC) ----
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

  const groupedRetros = useMemo<RetroGroup[]>(() => {
    const q = query.trim().toLowerCase()
    const filtered = retros
      .filter((r) => {
        if (retroStatus === 'all') return true
        if (retroStatus === 'open') return r.status === 'open' || r.status === 'in_review'
        return r.status === retroStatus
      })
      .filter((r) =>
        !q
          ? true
          : `${r.summary} ${r.kind} ${r.client} ${r.tool_name || ''}`.toLowerCase().includes(q)
      )
      .sort((a, b) => byCreatedDesc(a.created_at, b.created_at))

    const map = new Map<string, RetroGroup>()
    for (const item of filtered) {
      const stateBucket =
        item.status === 'open' || item.status === 'in_review' ? 'open' : item.status
      const key = `${stateBucket}::${canonicalRetroKey(item)}`
      const attempt = extractAttempt(item)
      const existing = map.get(key)
      if (!existing) {
        map.set(key, {
          key,
          ids: [item.retro_id],
          latest: item,
          count: 1,
          openCount: stateBucket === 'open' ? 1 : 0,
          maxAttempt: attempt,
          clients: [item.client],
          sessions: item.session_key ? [item.session_key] : [],
          advisory: item.advisory,
          hasQueuedSession: Boolean(item.has_queued_session),
        })
      } else {
        existing.ids.push(item.retro_id)
        existing.count += 1
        if (stateBucket === 'open') existing.openCount += 1
        if (attempt > existing.maxAttempt) existing.maxAttempt = attempt
        if (item.client && !existing.clients.includes(item.client)) {
          existing.clients.push(item.client)
        }
        if (item.session_key && !existing.sessions.includes(item.session_key)) {
          existing.sessions.push(item.session_key)
        }
        if (!existing.advisory && item.advisory) {
          existing.advisory = item.advisory
        }
        if (item.has_queued_session) {
          existing.hasQueuedSession = true
        }
      }
    }
    return Array.from(map.values()).sort((a, b) =>
      byCreatedDesc(a.latest.created_at, b.latest.created_at)
    )
  }, [retros, retroStatus, query])

  const openRetroGroupCount = useMemo(() => {
    const keys = new Set<string>()
    for (const r of retros) {
      if (r.status === 'open' || r.status === 'in_review') {
        keys.add(canonicalRetroKey(r))
      }
    }
    return keys.size
  }, [retros])

  const suggestedNoiseIds = useMemo(
    () =>
      groupedRetros
        .filter((g) => g.openCount > 0 && g.advisory?.verdict === 'ignore')
        .flatMap((g) => g.ids),
    [groupedRetros]
  )

  const actionPager = usePagination(actionItems, 10)
  const obsPager = usePagination(filteredObs, 10)
  const dissentPager = usePagination(filteredDissents, 10)
  const retroPager = usePagination(groupedRetros, 10)

  const runAction = async (fn: () => Promise<void>) => {
    setBusy(true)
    try {
      await fn()
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '操作失败，请稍后重试' })
    } finally {
      setBusy(false)
    }
  }

  const handlePromoteObservation = (obsId: string) =>
    runAction(async () => {
      await api.promoteObservation(obsId)
      await onChanged()
      toast.push('已将该错题升级为试运行避坑规则')
    })

  const handleArchiveObservation = (obsId: string) =>
    runAction(async () => {
      await api.archiveObservation(obsId)
      await onChanged()
      toast.push({
        message: '错题已归档',
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
      setSelectedObs([])
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
      setSelectedDissents([])
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
      setSelectedRetroKeys([])
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
      setSelectedRetroKeys([])
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
      setSelectedRetroKeys([])
      await onChanged()
      toast.push(`已将 ${targets.length} 组（共 ${resolvedTotal} 条）线索转入错题观察池并结案`)
    })

  const totalActionCount = highPriorityObs.length + openDissents.length

  const toggleSelect = (
    id: string,
    list: string[],
    setter: React.Dispatch<React.SetStateAction<string[]>>
  ) => {
    setter(list.includes(id) ? list.filter((x) => x !== id) : [...list, id])
  }

  return (
    <div className="space-y-4">
      {/* Sub-view Switcher */}
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

      {/* ==================== 2. 错题观察池 ==================== */}
      {subView === 'observations' && (
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
      )}

      {/* ==================== 3. 规则分歧 ==================== */}
      {subView === 'dissents' && (
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
      )}

      {/* ==================== 4. 实时异常线索 ==================== */}
      {subView === 'retros' && (
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
      )}
    </div>
  )
}
